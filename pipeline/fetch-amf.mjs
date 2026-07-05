import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

const BDIF_BASE = "https://bdif.amf-france.org";
const AMF_TAKEOVER_PAGE =
  "https://www.amf-france.org/en/professionals/professional-investors/my-relations-amf/takeover-reporting";

const { values } = parseArgs({
  options: {
    out: { type: "string", default: "outputs/amf-france-poc/run" },
    targets: {
      type: "string",
      default: "outputs/amf-france-poc/manual-offer-companies.json",
    },
    limit: { type: "string", default: "100" },
    all: { type: "boolean", default: false },
    "offer-list": { type: "boolean", default: true },
    query: { type: "string", default: "" },
    jeton: { type: "string", default: "" },
    "date-start": { type: "string", default: "" },
    "date-end": { type: "string", default: "" },
  },
});

const outDir = path.resolve(values.out);
const pdfDir = path.join(outDir, "pdfs");
const limit = Math.max(1, Number.parseInt(values.limit, 10) || 100);

function normaliseName(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toUpperCase()
    .replace(/\b(SOCIETE ANONYME FRANCAISE|SOCIETE ANONYME|SA|SAS|SCA|SE|PLC|LTD)\b/g, " ")
    .replace(/[^A-Z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: {
      accept: "application/json",
      "user-agent": "AMF OPA proof-of-concept scraper",
    },
  });

  if (!response.ok) {
    throw new Error(`GET ${url} failed with ${response.status}`);
  }

  return response.json();
}

async function fetchBuffer(url) {
  const response = await fetch(url, {
    headers: { "user-agent": "AMF OPA proof-of-concept scraper" },
  });

  if (!response.ok) {
    throw new Error(`GET ${url} failed with ${response.status}`);
  }

  return Buffer.from(await response.arrayBuffer());
}

async function loadTargets(filePath) {
  const raw = JSON.parse(await fs.readFile(path.resolve(filePath), "utf8"));
  const targets = Array.isArray(raw) ? raw : raw.targets ?? [];
  return targets.map((entry) => (typeof entry === "string" ? entry : entry.name));
}

function targetMatches(companyName, targetNames) {
  if (targetNames.length === 0) return true;

  const company = normaliseName(companyName);
  return targetNames.some((target) => {
    const wanted = normaliseName(target);
    return company === wanted || company.includes(wanted) || wanted.includes(company);
  });
}

function toRecord(item) {
  const target =
    item.societes?.find((company) => company.role === "SocieteConcernee") ??
    item.societes?.[0] ??
    {};
  const document =
    item.documents?.find((doc) => doc.accessible && doc.path) ?? item.documents?.[0] ?? {};
  const documentUrl = document.path
    ? `${BDIF_BASE}/back/api/v1/documents/${document.path}`
    : null;

  return {
    amf_number: item.numero,
    amf_reference: item.numeroConcatene ?? item.numero,
    title: item.titre,
    published_at: item.datePublication,
    online_at: item.dateMiseEnLigne,
    date_information: item.dateInformation,
    target: {
      token: target.jeton ?? null,
      name: target.raisonSociale ?? null,
      role: target.role ?? null,
    },
    types_information: item.typesInformation ?? [],
    types_document: item.typesDocument ?? [],
    document: {
      filename: document.nomFichier ?? `${item.numero}.pdf`,
      path: document.path ?? null,
      url: documentUrl,
      format: document.format ?? null,
    },
  };
}

async function downloadOfferList() {
  const html = (await fetchBuffer(AMF_TAKEOVER_PAGE)).toString("utf8");
  const match = html.match(/href="([^"]+\.xlsx[^"]*)"/i);
  if (!match) return null;

  const url = new URL(match[1].replace(/&amp;/g, "&"), AMF_TAKEOVER_PAGE).href;
  const filename = "amf-current-offer-companies.xlsx";
  await fs.writeFile(path.join(outDir, filename), await fetchBuffer(url));

  return { url, local_file: filename };
}

await fs.mkdir(pdfDir, { recursive: true });

const targetNames = values.all ? [] : await loadTargets(values.targets);
const params = new URLSearchParams({
  TypesInformation: "OPA",
  TypesDocument: "DeclarationAchatVente",
  From: "0",
  Size: String(limit),
});
if (values.query) {
  params.set("RechercheTexte", values.query);
}
if (values["date-start"]) {
  params.set("DateDebut", new Date(values["date-start"]).toISOString());
}
if (values["date-end"]) {
  params.set("DateFin", new Date(values["date-end"]).toISOString());
}
if (values.jeton) {
  values.jeton.split(",").map((value) => value.trim()).filter(Boolean).forEach((jeton) => {
    params.append("Jetons", jeton);
  });
}
const endpoint = `${BDIF_BASE}/back/api/v1/informations?${params.toString()}`;
const data = await fetchJson(endpoint);
const allRecords = (data.result ?? []).map(toRecord);
const filteredRecords = allRecords.filter((record) =>
  targetMatches(record.target.name, targetNames),
);

for (const record of filteredRecords) {
  if (!record.document.url) continue;
  const filename = `${record.amf_number}.pdf`;
  await fs.writeFile(path.join(pdfDir, filename), await fetchBuffer(record.document.url));
  record.document.local_pdf = path.join("pdfs", filename).replaceAll("\\", "/");
}

let offerList = null;
if (values["offer-list"]) {
  offerList = await downloadOfferList();
}

const payload = {
  generated_at: new Date().toISOString(),
  source: {
    bdif_endpoint: endpoint,
    amf_takeover_reporting_page: AMF_TAKEOVER_PAGE,
    offer_period_company_list: offerList,
  },
  filter: {
    mode: values.all ? "all_recent_opa_declaration_achat_vente" : "manual_offer_targets",
    limit,
    targets: targetNames,
    query: values.query || null,
    jetons: values.jeton ? values.jeton.split(",").map((value) => value.trim()).filter(Boolean) : [],
    date_start: values["date-start"] || null,
    date_end: values["date-end"] || null,
  },
  total_available_reported_by_bdif: data.total,
  pulled_count_before_target_filter: allRecords.length,
  matched_count: filteredRecords.length,
  filings: filteredRecords,
};

await fs.writeFile(
  path.join(outDir, "raw-filings.json"),
  JSON.stringify(payload, null, 2),
);

console.log(
  JSON.stringify(
    {
      outDir,
      total_available: data.total,
      pulled: allRecords.length,
      matched: filteredRecords.length,
      raw_file: path.join(outDir, "raw-filings.json"),
    },
    null,
    2,
  ),
);
