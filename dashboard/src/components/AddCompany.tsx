"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Both add paths, sharing one confirm step.
 *
 * The shape is deliberate: nothing is written until a human has looked at real
 * job titles from the board that was found. Token guessing is a heuristic and
 * it can land on somebody else's live board — "navi" is a plausible token for
 * more than one company — so the sample titles are the check that a count alone
 * cannot give you.
 */

type Candidate = {
  name: string;
  ats: string;
  token: string;
  config: Record<string, unknown>;
  category: string;
  india_scoped: boolean;
};

type Found = {
  found: true;
  candidate: Candidate;
  jobCount: number;
  /** Workday's total is the whole worldwide board, so it says so. */
  countLabel?: string;
  sampleTitles: string[];
  existing: { id: number; name: string; tracked: boolean } | null;
  note?: string;
  probesUsed?: number;
};

type NotFound = { found: false; message: string; tried?: string[]; parsed?: unknown };

export function AddCompany() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [urlName, setUrlName] = useState("");
  const [busy, setBusy] = useState<null | "name" | "url" | "save">(null);
  const [result, setResult] = useState<Found | NotFound | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function post(path: string, body: unknown) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
    return data;
  }

  async function run(kind: "name" | "url") {
    setBusy(kind);
    setResult(null);
    setSaved(null);
    setError(null);
    try {
      const data =
        kind === "name"
          ? await post("/api/companies/resolve", { name: name.trim() })
          : await post("/api/companies/parse-url", { url: url.trim(), name: urlName.trim() });
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function confirm(candidate: Candidate) {
    setBusy("save");
    setError(null);
    try {
      const data = await post("/api/companies", candidate);
      setSaved(data.message);
      setResult(null);
      setName("");
      setUrl("");
      setUrlName("");
      // The companies page is server-rendered; drop its cached payload so the
      // new row is there when you switch tabs.
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="panel">
        <h2>By name</h2>
        <p className="hint">
          Tries the company name as a board token, then a few mechanical variants, against
          greenhouse → lever → ashby. The first board that answers with jobs wins.
        </p>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) run("name");
          }}
        >
          <input
            type="text"
            value={name}
            placeholder="e.g. postman"
            onChange={(e) => setName(e.target.value)}
            aria-label="Company name"
          />
          <div className="fixed">
            <button className="primary" type="submit" disabled={!name.trim() || busy !== null}>
              {busy === "name" ? "Probing boards…" : "Look it up"}
            </button>
          </div>
        </form>
      </div>

      <div className="panel">
        <h2>By URL</h2>
        <p className="hint">
          A greenhouse / lever / ashby board URL, or a Workday careers URL
          (…myworkdayjobs.com/en-US/SiteName). Use this when the token is not a variant of the
          name — which is always the case for Workday.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim()) run("url");
          }}
        >
          <label className="field">
            <span>Board URL</span>
            <input
              type="url"
              value={url}
              placeholder="https://jobs.lever.co/acme"
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <div className="row">
            <label className="field" style={{ marginBottom: 0 }}>
              <span>Display name (optional — defaults to the token)</span>
              <input
                type="text"
                value={urlName}
                placeholder="Acme"
                onChange={(e) => setUrlName(e.target.value)}
              />
            </label>
            <div className="fixed">
              <button className="primary" type="submit" disabled={!url.trim() || busy !== null}>
                {busy === "url" ? "Checking board…" : "Parse & check"}
              </button>
            </div>
          </div>
        </form>
      </div>

      {error ? <p className="msg bad">{error}</p> : null}
      {saved ? <p className="msg ok">{saved}</p> : null}

      {result && !result.found ? (
        <div className="panel">
          <h2>Nothing stored</h2>
          <p className="msg warn" style={{ marginTop: 0 }}>
            {result.message}
          </p>
          {"tried" in result && result.tried?.length ? (
            <p className="small muted">Tokens tried: {result.tried.join(", ")}</p>
          ) : null}
        </div>
      ) : null}

      {result && result.found ? (
        <div className="panel">
          <h2>Found a board — confirm before it is stored</h2>
          <p className="hint">
            Check the titles below actually belong to the company you meant. Token guessing can
            land on somebody else&apos;s live board.
          </p>

          <table>
            <tbody>
              <tr>
                <td data-label="ATS">
                  <span className="muted small">ats</span>
                </td>
                <td data-label="">
                  <span className="tag">{result.candidate.ats}</span>
                </td>
              </tr>
              <tr>
                <td data-label="Token">
                  <span className="muted small">token</span>
                </td>
                <td data-label="">
                  <code>{result.candidate.token}</code>
                </td>
              </tr>
              <tr>
                <td data-label="Jobs">
                  <span className="muted small">{result.countLabel ?? "jobs on board"}</span>
                </td>
                <td data-label="" className="num">
                  {result.jobCount}
                </td>
              </tr>
              {Object.keys(result.candidate.config).length ? (
                <tr>
                  <td data-label="Config">
                    <span className="muted small">config</span>
                  </td>
                  <td data-label="">
                    <code className="small">{JSON.stringify(result.candidate.config)}</code>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>

          {result.note ? <p className="msg small">{result.note}</p> : null}

          {result.sampleTitles.length ? (
            <>
              <p className="hint" style={{ margin: "14px 0 6px" }}>
                Sample titles from that board:
              </p>
              <ul className="chips">
                {result.sampleTitles.map((t) => (
                  <li key={t} style={{ paddingRight: 11 }}>
                    {t}
                  </li>
                ))}
              </ul>
            </>
          ) : null}

          {result.existing ? (
            <p className="msg warn">
              Already in the table as <strong>{result.existing.name}</strong> (
              {result.existing.tracked ? "tracked" : "not tracked"}). Confirming refreshes that
              row and turns tracking on — it will not create a second one.
            </p>
          ) : null}

          <hr className="rule" />

          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              confirm(result.candidate);
            }}
          >
            <label className="field" style={{ marginBottom: 0 }}>
              <span>Store as</span>
              <input
                type="text"
                value={result.candidate.name}
                onChange={(e) =>
                  setResult({ ...result, candidate: { ...result.candidate, name: e.target.value } })
                }
              />
            </label>
            <label className="field fixed" style={{ marginBottom: 0, minWidth: 170 }}>
              <span>Category</span>
              <select
                value={result.candidate.category}
                onChange={(e) =>
                  setResult({
                    ...result,
                    candidate: { ...result.candidate, category: e.target.value },
                  })
                }
              >
                {["unknown", "product", "gcc", "services", "staffing"].map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <div className="fixed">
              <button
                className="primary"
                type="submit"
                disabled={busy !== null || !result.candidate.name.trim()}
              >
                {busy === "save" ? "Saving…" : "Add & track"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  );
}
