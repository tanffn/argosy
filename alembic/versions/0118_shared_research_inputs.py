"""Shared research sources, item versions, review requests and usage."""
from alembic import op

revision = "0118_shared_research_inputs"
down_revision = "0117_allocation_research_tasks"
branch_labels = None
depends_on = None

def upgrade():
    op.execute('\nCREATE TABLE research_sources (\n\tid INTEGER NOT NULL, \n\tuser_id VARCHAR(64) NOT NULL, \n\tname VARCHAR(256) NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\treference TEXT NOT NULL, \n\tconfig_json TEXT NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tcadence_hours INTEGER NOT NULL, \n\tpriority INTEGER NOT NULL, \n\tlast_polled_at DATETIME, \n\tlast_error TEXT, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_research_source UNIQUE (user_id, kind, reference), \n\tFOREIGN KEY(user_id) REFERENCES users (id)\n)\n\n')
    op.execute('CREATE INDEX ix_research_sources_user_id ON research_sources (user_id)')
    op.execute('\nCREATE TABLE research_items (\n\tid VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(64) NOT NULL, \n\tsource_id INTEGER NOT NULL, \n\texternal_id TEXT NOT NULL, \n\ttitle TEXT NOT NULL, \n\turl TEXT NOT NULL, \n\tauthor TEXT NOT NULL, \n\tpublished_at DATETIME, \n\tobserved_at DATETIME NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tbody TEXT NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tanalysis_json TEXT NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tattempted_at DATETIME, \n\tnext_attempt_at DATETIME, \n\tlease_until DATETIME, \n\tanalyzed_at DATETIME, \n\terror TEXT, \n\tcost_usd FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(source_id) REFERENCES research_sources (id)\n)\n\n')
    op.execute('CREATE INDEX ix_research_items_source_id ON research_items (source_id)')
    op.execute('CREATE INDEX ix_research_items_user_id ON research_items (user_id)')
    op.execute('CREATE INDEX ix_research_queue ON research_items (user_id, status, observed_at)')
    op.execute('\nCREATE TABLE research_claims (\n\tid VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(64) NOT NULL, \n\titem_id VARCHAR(64) NOT NULL, \n\tticker VARCHAR(32), \n\tscope VARCHAR(24) NOT NULL, \n\tstatement TEXT NOT NULL, \n\tpayload_json TEXT NOT NULL, \n\tdirection VARCHAR(16), \n\thorizon_days INTEGER, \n\tdue_at DATETIME, \n\toutcome_json TEXT, \n\tevaluated_at DATETIME, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(item_id) REFERENCES research_items (id)\n)\n\n')
    op.execute('CREATE INDEX ix_research_claims_due_at ON research_claims (due_at)')
    op.execute('CREATE INDEX ix_research_claims_item_id ON research_claims (item_id)')
    op.execute('CREATE INDEX ix_research_claims_ticker ON research_claims (ticker)')
    op.execute('CREATE INDEX ix_research_claims_user_id ON research_claims (user_id)')
    op.execute('\nCREATE TABLE research_review_requests (\n\tid VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(64) NOT NULL, \n\titem_id VARCHAR(64) NOT NULL, \n\tticker VARCHAR(32) NOT NULL, \n\treason TEXT NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tresult_json TEXT NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(item_id) REFERENCES research_items (id)\n)\n\n')
    op.execute('CREATE INDEX ix_research_review_requests_item_id ON research_review_requests (item_id)')
    op.execute('CREATE INDEX ix_research_review_requests_user_id ON research_review_requests (user_id)')
    op.execute('\nCREATE TABLE research_evidence_uses (\n\tid VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(64) NOT NULL, \n\titem_id VARCHAR(64) NOT NULL, \n\treport_id INTEGER NOT NULL, \n\tdecision_id VARCHAR(256), \n\tagent_role VARCHAR(128) NOT NULL, \n\tcited BOOLEAN NOT NULL, \n\tverdict VARCHAR(32), \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(item_id) REFERENCES research_items (id)\n)\n\n')
    op.execute('CREATE INDEX ix_research_evidence_uses_item_id ON research_evidence_uses (item_id)')
    op.execute('CREATE INDEX ix_research_evidence_uses_user_id ON research_evidence_uses (user_id)')

def downgrade():
    op.drop_table('research_evidence_uses')
    op.drop_table('research_review_requests')
    op.drop_table('research_claims')
    op.drop_table('research_items')
    op.drop_table('research_sources')
