"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type DomainKbFileResponse,
  type DomainKbReviewItem,
  type DomainKbTreeNode,
} from "@/lib/api";

interface FlatNode {
  depth: number;
  node: DomainKbTreeNode;
}

function flatten(root: DomainKbTreeNode | null, depth = 0): FlatNode[] {
  if (!root) return [];
  const out: FlatNode[] = [{ depth, node: root }];
  for (const c of root.children ?? []) {
    out.push(...flatten(c, depth + 1));
  }
  return out;
}

export default function DomainKbPage() {
  const [tree, setTree] = useState<DomainKbTreeNode | null>(null);
  const [file, setFile] = useState<DomainKbFileResponse | null>(null);
  const [queue, setQueue] = useState<DomainKbReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const [t, q] = await Promise.all([
        api.domainKbTree(),
        api.domainKbReviewQueue(),
      ]);
      setTree(t);
      setQueue(q.rows);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openFile = async (path: string) => {
    try {
      const f = await api.domainKbFile(path);
      setFile(f);
    } catch (e: unknown) {
      setError(String(e));
    }
  };

  const approve = async (id: number) => {
    await api.domainKbReviewApprove(id);
    refresh();
  };
  const reject = async (id: number) => {
    await api.domainKbReviewReject(id);
    refresh();
  };

  const flat = flatten(tree);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Domain knowledge</h1>
        <p className="text-sm text-muted-foreground">
          Browse the canonical rules and rates the agents cite. Refresh-agent
          proposals appear in the review queue for human approve / reject.
        </p>
      </header>

      {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
      {error && <p className="text-sm text-red-500 font-mono">{error}</p>}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Refresh review queue ({queue.length} pending)
          </CardTitle>
          <CardDescription>
            Proposed updates from the domain-refresh agent. Approval merges
            (Phase 7 surfaces them; integration with file-write lands later).
          </CardDescription>
        </CardHeader>
        <CardContent>
          {queue.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pending updates.</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {queue.map((q) => (
                <li
                  key={q.id}
                  className="border border-border rounded-md p-3 flex flex-col gap-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-sm">{q.path}</span>
                    <div className="flex gap-2">
                      <Button size="sm" onClick={() => approve(q.id)}>
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => reject(q.id)}
                      >
                        Reject
                      </Button>
                    </div>
                  </div>
                  <pre className="text-xs font-mono whitespace-pre-wrap text-muted-foreground">
                    {q.diff || "(no diff)"}
                  </pre>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Tree</CardTitle>
            <CardDescription>Click a file to view its contents.</CardDescription>
          </CardHeader>
          <CardContent className="max-h-[600px] overflow-y-auto">
            <ul className="text-sm font-mono">
              {flat.map(({ depth, node }) => (
                <li
                  key={node.path || "/"}
                  style={{ paddingLeft: depth * 12 }}
                  className={
                    node.is_dir
                      ? "py-1 text-muted-foreground"
                      : "py-1 cursor-pointer hover:text-primary"
                  }
                  onClick={() =>
                    !node.is_dir && node.path && openFile(node.path)
                  }
                >
                  {node.is_dir ? `[${node.name}]` : node.name}
                </li>
              ))}
              {flat.length === 0 && (
                <li className="text-muted-foreground">(empty)</li>
              )}
            </ul>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {file ? file.path : "Select a file"}
            </CardTitle>
            <CardDescription>
              Frontmatter is parsed; body is rendered raw.
            </CardDescription>
          </CardHeader>
          <CardContent className="max-h-[600px] overflow-y-auto">
            {file ? (
              <>
                {file.frontmatter && (
                  <pre className="text-xs font-mono whitespace-pre-wrap text-muted-foreground border border-border rounded p-2 mb-2">
                    {file.frontmatter}
                  </pre>
                )}
                <pre className="text-sm whitespace-pre-wrap">{file.content}</pre>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">No file selected.</p>
            )}
          </CardContent>
        </Card>
      </section>
    </main>
  );
}
