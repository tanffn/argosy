import { ConsultRunner } from "@/components/consult/consult-runner";

/**
 * /consult — ad-hoc ticker consultation. The implementation lives in the
 * reusable ``ConsultRunner`` (also mounted in the Proposals hub's "Ask the
 * team" entry); this route just frames it in a page <main>. Kept as a working
 * deep link.
 */
export default function ConsultPage() {
  return (
    <main className="max-w-5xl mx-auto p-6">
      <ConsultRunner />
    </main>
  );
}
