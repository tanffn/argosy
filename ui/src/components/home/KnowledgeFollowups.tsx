"use client";

import Link from "next/link";
import { knowledgeLabel, useDecisionReadiness } from "@/components/decision-readiness-banner";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function KnowledgeFollowups({ userId }: { userId: string }) {
  const state = useDecisionReadiness(userId);
  const knowledge = state?.knowledge;
  if (!knowledge) return null;
  const papers = knowledge.documents.filter((doc) => doc.input_matches && doc.state === "incomplete").flatMap((doc) => doc.findings
    .filter((finding) => finding.owner === "user" && finding.urgency !== "urgent")
    .map((finding, index) => ({ ...finding, key: `${doc.path}:${index}`, path: doc.path })));
  const due = knowledge.paperwork_due_date;
  return <Card aria-label="Documents and research follow-ups">
    <CardHeader>
      <CardTitle>Due papers</CardTitle>
      <CardDescription>
        {due ? `Planned for ${due} — your document collection target, not a legal deadline.` : "Provide when available."}
        {" "}Routine paperwork is not a blocker for portfolio monitoring or recommendations.
      </CardDescription>
    </CardHeader>
    <CardContent className="space-y-3">
      <details>
        <summary className="cursor-pointer font-medium">{papers.length} routine follow-ups — documents and why they help</summary>
        <ul className="mt-3 space-y-3">
          {papers.map((paper) => <li key={paper.key} className="border-l pl-3">
            <p>{paper.next_action}</p>
            <details className="mt-1 text-sm text-muted-foreground"><summary className="cursor-pointer">Why this helps</summary>
              <p>{paper.affected_advice}</p><p>Evidence: {paper.missing_evidence}</p>
            </details>
          </li>)}
        </ul>
      </details>
      <Link href="/files" className="text-sm underline">Open files / upload documents</Link>
      <details className="border-t pt-3 text-sm">
        <summary className="cursor-pointer">Argosy research &amp; evidence{knowledge.total !== undefined ? ` — ${knowledge.verified ?? 0}/${knowledge.total} documents verified` : " — status unavailable"}</summary>
        <p className="mt-2 text-muted-foreground">Incomplete verification is not a job failure. Argosy owns research and source repairs; specific limitations remain available below.</p>
        {knowledge.reason && <p>{knowledge.reason}</p>}
        {state.jobs?.some((job) => job.name === "knowledge_recheck" && job.status === "running") && <p>Argosy is reviewing queued documents now. No review action is needed from you.</p>}
        {knowledge.documents.map((doc) => <details key={doc.path} className="mt-2">
          <summary className="cursor-pointer">{doc.path} — {knowledgeLabel(doc.state)}</summary>
          {doc.review_reason && <p>{doc.review_reason}</p>}
          {doc.checked_at && <p>Last review: {new Date(doc.checked_at).toLocaleString()} · Report {doc.report_id}</p>}
          {doc.findings.map((finding, i) => <div key={i} className="my-2 border-l pl-3">
            <p>{finding.claim}</p><p>Missing: {finding.missing_evidence}</p>
            <p>Affects: {finding.affected_advice}</p>
            <p>Next action — {finding.owner === "user" ? "You" : "Argosy"}: {finding.next_action}</p>
          </div>)}
          <details><summary className="cursor-pointer">Full review explanation</summary><p className="whitespace-pre-wrap">{doc.note}</p></details>
        </details>)}
      </details>
    </CardContent>
  </Card>;
}
