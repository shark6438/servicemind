import { RunDetailView } from "@/components/runs/run-detail-view";

export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return <RunDetailView runId={runId} />;
}
