import { YouTubeSourcesPanel } from "@/components/input-sources/youtube-sources-panel";

export default function InputSourcesPage() {
  return (
    <main className="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <div>
        <p className="text-xs font-mono uppercase tracking-widest text-primary">Discovery</p>
        <h1 className="text-3xl font-semibold tracking-tight mt-1">Input Sources</h1>
        <p className="text-muted-foreground mt-2 max-w-3xl">
          Manage external research feeds that can nominate ideas for Argosy. YouTube videos are
          treated as unverified source material and pass through the claims, skeptic, portfolio,
          and synthesis fleet before anything reaches your watchlist or inbox.
        </p>
      </div>
      <YouTubeSourcesPanel />
    </main>
  );
}
