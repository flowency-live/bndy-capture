import readline from "node:readline";
import { CaptureStore } from "./db.js";
import { captureStatuses } from "./types.js";

const store = new CaptureStore();
const tools = [
  { name: "list_bndy_captures", description: "List items in the bndy capture backlog.", inputSchema: { type: "object", properties: { status: { type: "string", enum: captureStatuses }, limit: { type: "integer", minimum: 1, maximum: 500 } }, additionalProperties: false } },
  { name: "get_bndy_capture", description: "Get one capture including its raw payload.", inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"], additionalProperties: false } },
  { name: "update_bndy_capture_status", description: "Change a capture processing status.", inputSchema: { type: "object", properties: { id: { type: "string" }, status: { type: "string", enum: captureStatuses } }, required: ["id", "status"], additionalProperties: false } },
  { name: "add_bndy_capture_note", description: "Append a note to a capture.", inputSchema: { type: "object", properties: { id: { type: "string" }, note: { type: "string" } }, required: ["id", "note"], additionalProperties: false } }
];

const send = (value: unknown) => process.stdout.write(`${JSON.stringify(value)}\n`);
const text = (value: unknown) => ({ content: [{ type: "text", text: JSON.stringify(value, null, 2) }] });

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", line => {
  if (!line.trim()) return;
  let req: any;
  try { req = JSON.parse(line); } catch { return send({ jsonrpc: "2.0", error: { code: -32700, message: "Parse error" } }); }
  try {
    if (req.method === "initialize") return send({ jsonrpc: "2.0", id: req.id, result: { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "bndy-capture", version: "0.1.0" } } });
    if (req.method === "notifications/initialized") return;
    if (req.method === "tools/list") return send({ jsonrpc: "2.0", id: req.id, result: { tools } });
    if (req.method === "tools/call") {
      const name = String(req.params?.name ?? "");
      const args = req.params?.arguments ?? {};
      let value: unknown;
      if (name === "list_bndy_captures") value = store.list(args.status, args.limit ?? 100);
      else if (name === "get_bndy_capture") value = store.get(String(args.id));
      else if (name === "update_bndy_capture_status") value = store.updateStatus(String(args.id), args.status);
      else if (name === "add_bndy_capture_note") value = store.addNote(String(args.id), String(args.note));
      else throw new Error(`Unknown tool: ${name}`);
      return send({ jsonrpc: "2.0", id: req.id, result: text(value) });
    }
    send({ jsonrpc: "2.0", id: req.id, error: { code: -32601, message: "Method not found" } });
  } catch (error) {
    send({ jsonrpc: "2.0", id: req.id, error: { code: -32603, message: error instanceof Error ? error.message : "Internal error" } });
  }
});
