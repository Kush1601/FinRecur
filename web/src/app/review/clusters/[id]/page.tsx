import ClusterDetailClient from "./ClusterDetailClient";

export default async function ClusterPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ClusterDetailClient id={id} />;
}
