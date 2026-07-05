// Fetch AMF Art. 223-16 "total shares & voting rights" declarations from
// info-financiere.gouv.fr (the French OAM) for every company on the current
// AMF offer-period list, and download the declaration PDFs for parsing.
import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

const API =
  "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records";
const SUBTYPE = "Total du nombre de droits de vote et du capital";

const { values } = parseArgs({
  options: {
    state: { type: "string", default: "pipeline/state/filings.json" },
    out: { type: "string", default: "pipeline/tmp_capital" },
    "max-per-company": { type: "string", default: "40" },
  },
});

const outDir = path.resolve(values.out);
const pdfDir = path.join(outDir, "pdfs");
await fs.mkdir(pdfDir, { recursive: true });

const state = JSON.parse(await fs.readFile(path.resolve(values.state), "utf8"));
const companies = (state.offer_period_companies_from_amf_xlsx?.companies ?? []).filter(
  (c) => c.isin && c.offeree,
);

async function fetchJson(url) {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`GET ${url} failed with ${response.status}`);
  return response.json();
}

const results = [];
for (const company of companies) {
  const where = `identificationsociete_iso_cd_isi="${company.isin}" and sous_type_d_information="${SUBTYPE}"`;
  const url = `${API}?where=${encodeURIComponent(where)}&limit=${values["max-per-company"]}&order_by=informationdeposee_inf_dat_emt desc`;
  let records = [];
  try {
    records = (await fetchJson(url)).results ?? [];
  } catch (error) {
    results.push({ offeree: company.offeree, isin: company.isin, error: String(error) });
    continue;
  }
  const declarations = [];
  const seen = new Set();
  for (const record of records) {
    const pdfUrl = record.url_de_recuperation;
    if (!pdfUrl || seen.has(pdfUrl)) continue;
    seen.add(pdfUrl);
    const filename = pdfUrl.split("/").pop();
    const item = {
      emission_date: String(record.informationdeposee_inf_dat_emt || "").slice(0, 10),
      title: record.informationdeposee_inf_tit_inf || "",
      source_url: pdfUrl,
    };
    try {
      const response = await fetch(pdfUrl);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await fs.writeFile(path.join(pdfDir, filename), Buffer.from(await response.arrayBuffer()));
      item.local_pdf = path.join("pdfs", filename).replaceAll("\\", "/");
    } catch (error) {
      item.error = String(error);
    }
    declarations.push(item);
  }
  results.push({ offeree: company.offeree, isin: company.isin, declarations });
}

await fs.writeFile(
  path.join(outDir, "capital-declarations.json"),
  JSON.stringify({ generated_at: new Date().toISOString(), companies: results }, null, 2),
);
console.log(
  JSON.stringify({
    companies: companies.length,
    with_declarations: results.filter((r) => r.declarations?.length).length,
    pdf_errors: results.reduce(
      (n, r) => n + (r.declarations ?? []).filter((d) => d.error).length,
      0,
    ),
  }),
);
