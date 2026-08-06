/**
 * The resume keyword extractor, checked against text rather than argued about.
 *
 *     npm run skills-check
 *
 * Pure — no database, no server, no network, no running app. It exists because
 * lib/skills.ts is the one piece of real logic this slice added that nothing else
 * exercises: the routes around it are thin, the parsers are libraries, and the
 * chips are a component. A word-boundary rule that quietly stopped working would
 * show up as slightly worse suggestions, which is exactly the kind of wrong
 * nobody notices.
 *
 * THE CASE THAT MATTERS MOST is the substring one. `go` is inside "Django",
 * "Goa" and "ongoing"; `r` is inside every word in the language. notify/match.py
 * spends a docstring on why the naive `term in text` check is wrong in both
 * directions and fails silently, and this is the same rule one file over, so it
 * gets the same treatment: a test that would fail if the anchoring were dropped.
 */

import { extractSkills, SKILL_TERMS, SKILL_VOCABULARY } from "../src/lib/skills";

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

function has(text: string, term: string, limit = 25): boolean {
  return extractSkills(text, limit).includes(term);
}

console.log("\nvocabulary");
check(SKILL_TERMS.length > 400, `${SKILL_TERMS.length} canonical terms`, `${SKILL_TERMS.length}`);
const dupes = SKILL_TERMS.filter((t, i) => SKILL_TERMS.indexOf(t) !== i);
check(dupes.length === 0, "no duplicate canonical terms", dupes.join(", "));
const lowercase = SKILL_TERMS.filter((t) => t !== t.toLowerCase());
check(
  lowercase.length === 0,
  "every canonical term is lowercase — preferences.keywords is compared lowercased",
  lowercase.join(", "),
);
const bothForms = SKILL_VOCABULARY.filter((s) => s.strict && s.aliases);
check(
  bothForms.length === 0,
  "no entry carries both aliases and strict forms (strict wins and would drop them)",
  bothForms.map((s) => s.term).join(", "),
);

console.log("\nword boundaries, not substrings");
check(!has("Experienced with Django and Goa tourism", "go"), '"Django"/"Goa" do NOT yield `go`');
check(has("Backend services in Go and Python", "go"), '"Go" as a word DOES yield `go`');
check(!has("Ongoing work on a good product, going forward. ".repeat(6), "go"), '"ongoing"/"going" do not');
check(!has("Rapid growth at Capital One, already a leader in Qatar", "api"), '"Rapid" does not yield `api`');
check(has("Designed REST APIs and an API gateway", "api"), '"API" does');
check(has("Wrote C++ and C# services", "c++"), "`c++` survives its punctuation edge");
check(has("Built on .NET Core", ".net"), "`.net` matches without a leading word boundary");
check(has("Node.js and Express on the backend", "node"), "`node` matches inside Node.js");

console.log("\nmulti-word terms across separators");
for (const written of ["machine learning", "machine-learning", "machine  learning"]) {
  check(has(`Applied ${written} to ranking`, "machine learning"), `"${written}" -> machine learning`);
}
check(
  has("Skills\nMachine\nlearning and deep learning", "machine learning"),
  "a term split across a line break still matches",
);

console.log("\nthe ambiguous ones need their real spelling");
check(!has("We go the extra mile and ship fast", "go"), "prose 'go' is not the language");
check(!has("A rust-free finish on the hardware", "rust"), "lowercase 'rust' is not the language");
check(has("Systems programming in Rust", "rust"), "capitalised Rust is");
check(has("Built the API with Express and Node", "express"), "'Express' capitalised is");
check(!has("Dr. R Kumar supervised the project", "r"), "an initial is not the R language");
check(has("Languages: Python, R, Scala", "r"), "R in a delimited skills list is");
check(!has("Spring 2024 internship at a startup", "spring"), "'Spring 2024' is a date, not the framework");
check(!has("Joined in Fall 2023 and left Spring 2025", "spring"), "and so is 'Spring 2025'");
check(has("Spring Boot microservices with Hibernate", "spring"), "'Spring Boot' still is the framework");

/*
 * A KNOWN AND ACCEPTED FALSE POSITIVE, asserted so it is a decision rather than
 * a surprise. "Express delivery" at the start of a sentence is capitalised
 * exactly like the framework, and nothing short of reading the sentence can
 * separate them. The cost is one chip a person deletes in one click; the cost of
 * "fixing" it would be a blocklist of English phrases, which would be guessing
 * about language rather than matching against a vocabulary.
 *
 * This is written as an assertion so that a future change to the strict rules
 * that happens to fix it fails here and gets the comment updated, rather than
 * silently drifting.
 */
check(
  has("Express delivery and same-day shipping", "express"),
  "ACCEPTED: sentence-initial 'Express' is a known false positive (one click to remove)",
);

console.log("\nranking and shape");
const resume = `
  Priya Sharma — Backend Engineer
  Skills: Python, Django, PostgreSQL, Redis, Docker, Kubernetes
  Experience: Built Python services on Kubernetes. Python data pipelines with
  Airflow. Migrated Postgres schemas. More Python work on internal tooling.
`;
const ranked = extractSkills(resume);
check(ranked[0] === "python", `most-repeated skill ranks first (got "${ranked[0]}")`, ranked.join(", "));
check(ranked.includes("postgresql"), '"Postgres" collapses onto the canonical `postgresql`');
check(new Set(ranked).size === ranked.length, "no duplicates in the result", ranked.join(", "));
check(extractSkills(resume, 3).length === 3, "the limit is honoured");
check(extractSkills("").length === 0, "empty text yields nothing");
check(extractSkills("   \n  ").length === 0, "whitespace-only text yields nothing");
check(
  extractSkills("A resume about gardening and poetry with no technology in it").length === 0,
  "text with no skills in it yields nothing rather than guessing",
);

// Aliases collapse rather than double-counting: a resume naming both spellings
// must produce one chip, or the cap is spent twice on one skill.
const aliased = extractSkills("Used Postgres heavily. PostgreSQL tuning and psql scripting.");
check(
  aliased.filter((t) => t === "postgresql").length === 1,
  "three spellings of one skill produce exactly one chip",
  aliased.join(", "),
);

console.log("\nbreadth — a vocabulary that covers more than one kind of engineer");
const profiles: Record<string, string> = {
  frontend: "React, TypeScript, Next.js, Tailwind CSS, Webpack and Jest",
  data: "Airflow, dbt, Snowflake, Spark and Kafka pipelines",
  ml: "PyTorch, scikit-learn, MLflow, Hugging Face transformers and CUDA",
  mobile: "Swift, SwiftUI, Kotlin, Jetpack Compose and React Native",
  infra: "Terraform, Kubernetes, Helm, Prometheus, Grafana and ArgoCD",
  security: "OWASP, penetration testing, TLS, SIEM and threat modeling",
};
for (const [name, text] of Object.entries(profiles)) {
  const found = extractSkills(text);
  check(found.length >= 5, `${name}: ${found.length} skills found`, found.join(", "));
}

console.log(failures === 0 ? "\nAll checks passed.\n" : `\n${failures} check(s) FAILED.\n`);
process.exit(failures === 0 ? 0 : 1);
