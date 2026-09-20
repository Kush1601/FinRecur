export default function NotAvailable({ label }: { label?: string }) {
  return (
    <div className="not-available">
      {label ?? "This section could not be loaded."}
    </div>
  );
}

export function SimulatedTag() {
  return <span className="tag tag-simulated">simulated</span>;
}
