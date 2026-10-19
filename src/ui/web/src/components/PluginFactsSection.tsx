import type { Credit, PluginFacts } from "../api/types";

/** Who to credit, linked, and the license it names ("GeoNames · CC BY 4.0"): beside a plug-in's facts or places,
 * and a place a person picked. */
export function CreditLinks({ credit, className = "hover:underline" }: { credit: Credit; className?: string }) {
  return (
    <>
      {credit.url ? (
        <a href={credit.url} target="_blank" rel="noopener noreferrer" className={className}>
          {credit.text}
        </a>
      ) : (
        credit.text
      )}
      {credit.license && (
        <>
          {" · "}
          <a href={credit.license.url} target="_blank" rel="noopener noreferrer" className={className}>
            {credit.license.text}
          </a>
        </>
      )}
    </>
  );
}

/** What installed plug-ins' work says of a clip (ADR-023), in the Lightbox: a section for each plug-in on here
 * that made it a fact, under the plug-in's title, each fact beside its label (a link when it has one; its empty
 * line, muted, when it had nothing to say), and who to credit. Nothing of a plug-in that's off, and nothing on a
 * public page (the server sends none). */
export function PluginFactsSection({ plugins }: { plugins: PluginFacts[] }) {
  return (
    <>
      {plugins.map((p) => (
        <section key={p.plugin_id} aria-label={p.title}>
          <hr className="mb-4 border-gray-700" />
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">{p.title}</div>
          <dl className="space-y-2">
            {p.facts.map((f) => (
              <div key={f.key} className="flex">
                <dt className="w-2/5 text-xs text-gray-500">{f.label}</dt>
                <dd className={`w-3/5 text-sm ${f.empty ? "text-gray-500" : "text-gray-300"}`}>
                  {f.url ? (
                    <a href={f.url} target="_blank" rel="noopener noreferrer" className="text-indigo-300 hover:underline">
                      {f.text}
                    </a>
                  ) : (
                    f.text
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {p.credit && (
            <p className="mt-2 text-xs text-gray-500">
              From <CreditLinks credit={p.credit} />
            </p>
          )}
        </section>
      ))}
    </>
  );
}
