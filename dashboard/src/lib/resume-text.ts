/**
 * A file the browser sent -> the text inside it, or a refusal a person can act
 * on. Server-only: it imports two parsers that need Node.
 *
 * EVERY FAILURE HERE IS A 4xx WITH A SENTENCE, NEVER A 500. That is the whole
 * point of the file. Uploading a resume is the first thing a new account does,
 * the inputs are entirely outside our control — a scan from a photocopier, a
 * password-protected PDF an employer sent, a .pages file renamed to .pdf — and
 * "Something went wrong on the server" tells a person nothing about a problem
 * that is on their side and fixable in one action. So each way this can fail is
 * a named code carrying a message written for the person who hit it, and the
 * route maps codes to statuses rather than catching a bare Error.
 *
 * THE TYPE IS DECIDED BY THE BYTES, NOT BY WHAT THE BROWSER CLAIMED. File.type
 * comes from the client and a filename extension is a string anybody can type.
 * Neither is evidence. Both are still CHECKED — a mismatch is a clearer message
 * than a parse failure — but what actually selects the parser is the magic
 * number at the front of the file.
 */

export type ResumeKind = "pdf" | "docx";

export const MAX_BYTES = 5 * 1024 * 1024; // 5 MB, matching the bucket's own limit

export const CONTENT_TYPES: Record<ResumeKind, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

/**
 * How much text makes a parse believable.
 *
 * A resume is at least a few hundred characters. Below that the likely causes
 * are a scan with no text layer, a PDF that is one big image, or a file whose
 * text is in a form the extractor could not reach — and all three are the same
 * message to the person holding it: we could not read this, send a text one.
 *
 * Deliberately not zero. An image-only PDF frequently yields a handful of
 * characters rather than none at all — a page number, a footer, an artefact of
 * the scanner — and a zero-length check would call that a successful parse and
 * then suggest no keywords with no explanation.
 */
const MIN_TEXT_CHARS = 200;

export type ResumeErrorCode =
  | "too_large"
  | "empty_file"
  | "unsupported_type"
  | "type_mismatch"
  | "encrypted"
  | "unreadable"
  | "no_text";

/** Which HTTP status each refusal deserves. */
export const STATUS_FOR: Record<ResumeErrorCode, number> = {
  too_large: 413,
  empty_file: 400,
  unsupported_type: 415,
  type_mismatch: 415,
  encrypted: 422,
  unreadable: 422,
  no_text: 422,
};

export class ResumeError extends Error {
  readonly code: ResumeErrorCode;
  constructor(code: ResumeErrorCode, message: string) {
    super(message);
    this.name = "ResumeError";
    this.code = code;
  }
}

/**
 * The first bytes of the file, which is the only thing that decides.
 *
 * PDF  — "%PDF-", per the spec's own header.
 * DOCX — "PK\x03\x04": it is a zip, and so is every other OOXML file. That is
 *        as far as sniffing can go, and it is far enough: a .xlsx renamed to
 *        .docx passes this and then fails in mammoth, which is reported as
 *        `unreadable` with a message that says the file could not be read as a
 *        Word document. Claiming to detect more precision than a zip header
 *        offers would be inventing certainty.
 */
function sniff(bytes: Uint8Array): ResumeKind | null {
  const starts = (sig: readonly number[]) =>
    sig.length <= bytes.length && sig.every((b, i) => bytes[i] === b);

  if (starts([0x25, 0x50, 0x44, 0x46, 0x2d])) return "pdf"; // %PDF-
  if (starts([0x50, 0x4b, 0x03, 0x04])) return "docx"; //     PK..
  return null;
}

/** What the filename claims, for the mismatch message only. */
function claimedKind(filename: string, declaredType: string): ResumeKind | null {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".pdf") || declaredType === CONTENT_TYPES.pdf) return "pdf";
  if (lower.endsWith(".docx") || declaredType === CONTENT_TYPES.docx) return "docx";
  return null;
}

const LABEL: Record<ResumeKind, string> = { pdf: "PDF", docx: "Word (.docx)" };

/**
 * A PDF that will not open without a password.
 *
 * pdf.js signals this with a named exception rather than a status code, and the
 * name is stable across versions in a way the message is not. Both are checked,
 * because an unrecognised encrypted file falling through to `unreadable` would
 * tell somebody their file is corrupt when it is merely locked — and those have
 * completely different next actions.
 */
function looksEncrypted(err: unknown): boolean {
  const name = (err as { name?: string } | null)?.name ?? "";
  const message = String((err as { message?: string } | null)?.message ?? "");
  return name === "PasswordException" || /password/i.test(message);
}

async function pdfText(bytes: Uint8Array): Promise<string> {
  const { extractText, getDocumentProxy } = await import("unpdf");
  try {
    const doc = await getDocumentProxy(bytes);
    const { text } = await extractText(doc, { mergePages: true });
    return text;
  } catch (err) {
    if (looksEncrypted(err)) {
      throw new ResumeError(
        "encrypted",
        "This PDF is password-protected, so its text cannot be read. Open it, " +
          "save an unprotected copy, and upload that.",
      );
    }
    throw new ResumeError(
      "unreadable",
      "This file could not be read as a PDF. It may be damaged, or it may be a " +
        "different kind of file that was renamed. Try re-exporting it from the " +
        "app you wrote it in.",
    );
  }
}

async function docxText(bytes: Uint8Array): Promise<string> {
  const mammoth = (await import("mammoth")).default;
  try {
    // Buffer.from(view) copies rather than aliasing, which matters because
    // mammoth hands the buffer to a zip reader that may hold it past this call.
    const { value } = await mammoth.extractRawText({ buffer: Buffer.from(bytes) });
    return value;
  } catch {
    throw new ResumeError(
      "unreadable",
      "This file could not be read as a Word document. If it is an older .doc, " +
        "open it and save it as .docx, then upload that.",
    );
  }
}

export type ParsedResume = {
  readonly kind: ResumeKind;
  readonly contentType: string;
  readonly text: string;
  readonly bytes: Uint8Array;
  readonly size: number;
};

/**
 * The whole of it: validate, sniff, parse, and insist the result is real text.
 *
 * The order is deliberate — the cheap refusals come first, so an oversized file
 * is rejected on its length rather than after being pushed through a PDF parser.
 */
export async function parseResume(file: File): Promise<ParsedResume> {
  if (file.size === 0) {
    throw new ResumeError("empty_file", "That file is empty — there is nothing in it to read.");
  }
  if (file.size > MAX_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1);
    throw new ResumeError(
      "too_large",
      `That file is ${mb} MB. The limit is 5 MB — a text-based resume is almost ` +
        `always under 1 MB, so a file this size is usually a scan.`,
    );
  }

  const bytes = new Uint8Array(await file.arrayBuffer());
  const kind = sniff(bytes);

  if (!kind) {
    const claimed = claimedKind(file.name, file.type);
    if (claimed) {
      throw new ResumeError(
        "type_mismatch",
        `This is named like a ${LABEL[claimed]} file but its contents are not one. ` +
          `It may have been renamed rather than converted — re-export it as a real ` +
          `${LABEL[claimed]} and upload that.`,
      );
    }
    throw new ResumeError(
      "unsupported_type",
      "Only PDF and Word (.docx) resumes can be read. If yours is a Google Doc " +
        "or Pages file, export it as a PDF first.",
    );
  }

  const text = kind === "pdf" ? await pdfText(bytes) : await docxText(bytes);
  const trimmed = text.replace(/\s+/g, " ").trim();

  if (trimmed.length < MIN_TEXT_CHARS) {
    throw new ResumeError(
      "no_text",
      kind === "pdf"
        ? "No readable text was found in this PDF. It is most likely a scan or an " +
          "image of a resume rather than a text one — export it from the app you " +
          "wrote it in instead of scanning a printout."
        : "No readable text was found in this document. If the content is inside " +
          "images or text boxes, the text cannot be extracted from it.",
    );
  }

  return {
    kind,
    contentType: CONTENT_TYPES[kind],
    // The ORIGINAL text, newlines and all, is what goes to the extractor:
    // lib/skills matches multi-word terms across whitespace, and the collapsed
    // copy above exists only to count characters honestly.
    text,
    bytes,
    size: file.size,
  };
}
