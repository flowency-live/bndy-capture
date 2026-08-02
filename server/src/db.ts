import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { CaptureStatus, EntityType } from "./types.js";

export interface Capture {
  id: string;
  capturedAt: string;
  receivedAt: string;
  sharedText: string | null;
  sharedUrl: string | null;
  mimeType: string;
  sourceApp: string | null;
  note: string | null;
  suggestedEntityType: EntityType;
  status: CaptureStatus;
  rawPayload: Record<string, unknown> | null;
  updatedAt: string;
}

export interface NewCapture {
  capturedAt?: string;
  sharedText?: string | null;
  sharedUrl?: string | null;
  mimeType: string;
  sourceApp?: string | null;
  note?: string | null;
  suggestedEntityType: EntityType;
  rawPayload?: Record<string, unknown>;
}

export class CaptureStore {
  private db: Database.Database;

  constructor(dbPath = process.env.BNDY_CAPTURE_DB ?? "./data/captures.db") {
    const resolved = path.resolve(dbPath);
    fs.mkdirSync(path.dirname(resolved), { recursive: true });
    this.db = new Database(resolved);
    this.db.pragma("journal_mode = WAL");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS captures (
        id TEXT PRIMARY KEY,
        captured_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        shared_text TEXT,
        shared_url TEXT,
        mime_type TEXT NOT NULL,
        source_app TEXT,
        note TEXT,
        suggested_entity_type TEXT NOT NULL,
        status TEXT NOT NULL,
        raw_payload TEXT,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_captures_status_received
      ON captures(status, received_at DESC);
    `);
  }

  create(input: NewCapture): Capture {
    const now = new Date().toISOString();
    const capture: Capture = {
      id: randomUUID(),
      capturedAt: input.capturedAt ?? now,
      receivedAt: now,
      sharedText: input.sharedText ?? null,
      sharedUrl: input.sharedUrl ?? null,
      mimeType: input.mimeType,
      sourceApp: input.sourceApp ?? null,
      note: input.note ?? null,
      suggestedEntityType: input.suggestedEntityType,
      status: "unprocessed",
      rawPayload: input.rawPayload ?? null,
      updatedAt: now
    };

    this.db.prepare(`
      INSERT INTO captures (
        id, captured_at, received_at, shared_text, shared_url, mime_type,
        source_app, note, suggested_entity_type, status, raw_payload, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      capture.id, capture.capturedAt, capture.receivedAt, capture.sharedText,
      capture.sharedUrl, capture.mimeType, capture.sourceApp, capture.note,
      capture.suggestedEntityType, capture.status,
      capture.rawPayload ? JSON.stringify(capture.rawPayload) : null, capture.updatedAt
    );
    return capture;
  }

  list(status?: CaptureStatus, limit = 100): Capture[] {
    const safeLimit = Math.max(1, Math.min(limit, 500));
    const rows = status
      ? this.db.prepare("SELECT * FROM captures WHERE status = ? ORDER BY received_at DESC LIMIT ?").all(status, safeLimit)
      : this.db.prepare("SELECT * FROM captures ORDER BY received_at DESC LIMIT ?").all(safeLimit);
    return rows.map(row => this.map(row as Record<string, unknown>));
  }

  get(id: string): Capture | null {
    const row = this.db.prepare("SELECT * FROM captures WHERE id = ?").get(id);
    return row ? this.map(row as Record<string, unknown>) : null;
  }

  updateStatus(id: string, status: CaptureStatus): Capture | null {
    const updatedAt = new Date().toISOString();
    const result = this.db.prepare("UPDATE captures SET status = ?, updated_at = ? WHERE id = ?")
      .run(status, updatedAt, id);
    return result.changes ? this.get(id) : null;
  }

  addNote(id: string, note: string): Capture | null {
    const current = this.get(id);
    if (!current) return null;
    const combined = current.note ? `${current.note}\n\n${note}` : note;
    const updatedAt = new Date().toISOString();
    this.db.prepare("UPDATE captures SET note = ?, updated_at = ? WHERE id = ?")
      .run(combined, updatedAt, id);
    return this.get(id);
  }

  private map(row: Record<string, unknown>): Capture {
    return {
      id: String(row.id),
      capturedAt: String(row.captured_at),
      receivedAt: String(row.received_at),
      sharedText: row.shared_text == null ? null : String(row.shared_text),
      sharedUrl: row.shared_url == null ? null : String(row.shared_url),
      mimeType: String(row.mime_type),
      sourceApp: row.source_app == null ? null : String(row.source_app),
      note: row.note == null ? null : String(row.note),
      suggestedEntityType: String(row.suggested_entity_type) as EntityType,
      status: String(row.status) as CaptureStatus,
      rawPayload: row.raw_payload ? JSON.parse(String(row.raw_payload)) : null,
      updatedAt: String(row.updated_at)
    };
  }
}
