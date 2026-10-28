import json
from datetime import UTC,datetime,timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from argosy.state.research_models import ResearchItem,ResearchSource
from argosy.services.allocation_author.research_inputs import collect_allocation_research


def test_recent_reviewed_evidence_and_missing_coverage_are_separate():
    now=datetime(2026,10,14,15,tzinfo=UTC)
    engine=create_engine('sqlite://')
    ResearchSource.__table__.create(engine)
    ResearchItem.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([ResearchSource(id=1,user_id='a',name='Source A',kind='youtube',reference='a'),
                    ResearchSource(id=2,user_id='b',name='Private B',kind='youtube',reference='b')])
        db.flush()
        def item(key,**over):
            data=dict(id=key*64,user_id='a',source_id=1,external_id=key,title=key,url='https://example.org/'+key,
                      observed_at=now-timedelta(hours=1),analyzed_at=now,status='analyzed',
                      body='',content_hash=key*64,analysis_json=json.dumps({'evidence_review':{'status':'approved'},
                          'synthesis':{'executive_summary':'Source claims inflation is persistent','bottom_line':'Verify first'}}))
            data.update(over);return ResearchItem(**data)
        db.add_all([item('a'),item('b',user_id='b',source_id=2),
                    item('c',observed_at=now,status='queued',analyzed_at=None),
                    item('d',observed_at=now-timedelta(days=9)),
                    item('e',analysis_json='{}'),
                    item('f',published_at=now+timedelta(days=1))])
        db.commit()
        result=collect_allocation_research(db,'a',now=now)
        assert [i['item_id'] for i in result['items']]==['a'*64]
        assert result['items'][0]['source_id']=='research:'+'a'*64
        assert result['items'][0]['synthesis']['executive_summary']
        assert result['coverage'][0]['latest_status']=='queued'
        assert len(result['coverage'])==1
        assert not collect_allocation_research(db,'a',now=now,limit=0)['items']
        # Recent observation cannot revive stale/future browser source dates.
        for source_date in ['2026-01-01', '2026-10-15', 'not-a-date']:
            record=db.get(ResearchItem,'a'*64)
            payload=json.loads(record.analysis_json)
            payload['capture']={'source_date':source_date}
            record.analysis_json=json.dumps(payload);db.commit()
            result=collect_allocation_research(db,'a',now=now)
            assert not result['items']
            assert result['excluded_stale_or_future_source_dates']==1
        payload.pop('capture');record.analysis_json=json.dumps(payload);db.commit()
        # Queued traffic must not hide approved evidence or another source.
        db.add_all([item(str(i).zfill(64)[-64:], id=str(i).zfill(64), status='queued',
                          observed_at=now,analyzed_at=None) for i in range(210)])
        db.commit()
        result=collect_allocation_research(db,'a',now=now)
        assert [i['item_id'] for i in result['items']]==['a'*64]
        payload['synthesis']={'executive_summary':'x'*1000000,
            'market_outlook':[{str(i):'z'*10000 for i in range(1000)}]}
        record.analysis_json=json.dumps(payload);db.commit()
        result=collect_allocation_research(db,'a',now=now)
        assert len(json.dumps(result))<10000
        assert result['items'][0]['truncated']
        db.add_all([ResearchSource(id=i,user_id='a',name='\U0001f600'*200,
            kind='youtube',reference=str(i),last_error='\U0001f600'*200) for i in range(3,35)])
        db.commit()
        result=collect_allocation_research(db,'a',now=now)
        assert len(json.dumps(result))<=60000
        assert result['omitted_sources']>0


def test_author_and_blind_reviewer_receive_same_attributed_research():
    from argosy.agents.deployment_author import DeploymentAuthorAgent
    from argosy.agents.deployment_reviewer import DeploymentReviewerAgent
    from tests.test_deployment_author_agent import _packet
    packet=_packet()
    packet['subscription_research']={'guidance':'Unverified source claim',
        'items':[{'source_id':'research:'+'a'*64,'title':'Recent source finding'}]}
    _,author=DeploymentAuthorAgent(user_id='a').build_prompt(packet=packet,feedback=None)
    _,review=DeploymentReviewerAgent(user_id='a').build_prompt(lens='prudence',packet=packet,buys=[])
    for prompt in [author,review]:
        assert 'Recent source finding' in prompt
        assert 'research:'+'a'*64 in prompt


def test_both_agents_receive_same_canonical_fund_reference_despite_old_history():
    from argosy.agents.deployment_author import DeploymentAuthorAgent
    from argosy.agents.deployment_reviewer import DeploymentReviewerAgent
    from tests.test_deployment_author_agent import _packet
    from argosy.services.allocation_research import fund_research_evidence
    def result(date, report, conviction):
        return {'as_of': date, 'tickers': {'IWQU': {'kind': 'fund_vehicle',
            'report_id': report, 'verdict': 'HOLD', 'conviction': conviction}}}
    tasks = [{'tickers': ['IWQU'], 'research_result': {'prior_results': [
        result('2026-10-09T16:00:00+00:00', 10, 'HIGH'),
        result('2026-10-14T15:47:16+00:00', 20, 'MED')]}}]
    packet = _packet()
    packet['allocation_research_tasks'] = tasks
    expected = fund_research_evidence(tasks)
    assert expected['IWQU']['report_id'] == 20
    _, author = DeploymentAuthorAgent(user_id='a').build_prompt(packet=packet, feedback=None)
    _, review = DeploymentReviewerAgent(user_id='a').build_prompt(lens='prudence', packet=packet, buys=[])
    for prompt in [author, review]:
        tail = prompt.split('CANONICAL FUND EVIDENCE FOR THIS PACKET', 1)[1]
        record, _ = json.JSONDecoder().raw_decode(tail.split('\n', 1)[1])
        assert record == expected
