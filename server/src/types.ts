import { z } from "zod";

export const entityTypes = ["unknown", "venue", "artist", "event"] as const;
export const captureStatuses = ["unprocessed", "processing", "processed", "rejected"] as const;

export const createCaptureSchema = z.object({
  capturedAt: z.string().datetime().optional(),
  sharedText: z.string().max(100_000).nullable().optional(),
  sharedUrl: z.string().url().max(10_000).nullable().optional(),
  mimeType: z.string().max(255).default("text/plain"),
  sourceApp: z.string().max(255).nullable().optional(),
  note: z.string().max(20_000).nullable().optional(),
  suggestedEntityType: z.enum(entityTypes).default("unknown"),
  rawPayload: z.record(z.unknown()).optional()
}).refine(v => Boolean(v.sharedText || v.sharedUrl || v.rawPayload), {
  message: "At least one shared value is required"
});

export const updateStatusSchema = z.object({ status: z.enum(captureStatuses) });
export const addNoteSchema = z.object({ note: z.string().max(20_000) });
export type CaptureStatus = typeof captureStatuses[number];
export type EntityType = typeof entityTypes[number];
