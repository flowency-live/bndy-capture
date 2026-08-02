import express from "express";
import cors from "cors";
import { CaptureStore } from "./db.js";
import { addNoteSchema, captureStatuses, createCaptureSchema, updateStatusSchema } from "./types.js";
import { firstUrl } from "./url.js";

const app = express();
const store = new CaptureStore();
const token = process.env.BNDY_CAPTURE_TOKEN ?? "change-me";
const port = Number(process.env.PORT ?? 8787);

app.use(cors());
app.use(express.json({ limit: "2mb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "bndy-capture", time: new Date().toISOString() });
});

app.use("/v1", (req, res, next) => {
  if (req.header("authorization") !== `Bearer ${token}`) {
    res.status(401).json({ error: "unauthorised" });
    return;
  }
  next();
});

app.post("/v1/captures", (req, res) => {
  const parsed = createCaptureSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid_capture", details: parsed.error.flatten() });
    return;
  }
  const data = parsed.data;
  res.status(201).json(store.create({ ...data, sharedUrl: data.sharedUrl ?? firstUrl(data.sharedText) }));
});

app.get("/v1/captures", (req, res) => {
  const value = typeof req.query.status === "string" ? req.query.status : undefined;
  const status = captureStatuses.includes(value as never) ? value as typeof captureStatuses[number] : undefined;
  const limit = Number(req.query.limit ?? 100);
  res.json({ items: store.list(status, Number.isFinite(limit) ? limit : 100) });
});

app.get("/v1/captures/:id", (req, res) => {
  const item = store.get(req.params.id);
  if (!item) return res.status(404).json({ error: "not_found" });
  res.json(item);
});

app.patch("/v1/captures/:id/status", (req, res) => {
  const parsed = updateStatusSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid_status" });
  const item = store.updateStatus(req.params.id, parsed.data.status);
  if (!item) return res.status(404).json({ error: "not_found" });
  res.json(item);
});

app.post("/v1/captures/:id/notes", (req, res) => {
  const parsed = addNoteSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid_note" });
  const item = store.addNote(req.params.id, parsed.data.note);
  if (!item) return res.status(404).json({ error: "not_found" });
  res.json(item);
});

app.listen(port, "0.0.0.0", () => console.log(`bndy Capture API listening on :${port}`));
