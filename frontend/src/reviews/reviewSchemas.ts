import { z } from "zod";

const controlCharacter = /\p{Cc}/u;

const safeText = (maxLength: number) =>
  z
    .string()
    .min(1, "请填写说明")
    .max(maxLength, `说明不能超过 ${maxLength} 个字符`)
    .refine((value) => !controlCharacter.test(value), "说明不能包含控制字符");

export const samplingConfigSchema = z
  .object({
    expected_current_version: z.number().int().min(0),
    rate_bps: z.number().int().min(1).max(10_000),
    min_sample_size: z.number().int().min(1),
    max_sample_size: z.number().int().min(1),
    change_reason: safeText(500),
  })
  .refine((value) => value.max_sample_size >= value.min_sample_size, {
    message: "最大样本数不能小于最小样本数",
    path: ["max_sample_size"],
  });

export const findingDecisionSchema = z
  .object({
    kind: z.literal("finding"),
    decision: z.enum(["confirmed", "false_positive"]),
    note: z.string().max(2_000, "说明不能超过 2000 个字符").optional(),
  })
  .superRefine((value, context) => {
    if (value.note && controlCharacter.test(value.note)) {
      context.addIssue({ code: "custom", path: ["note"], message: "说明不能包含控制字符" });
    }
    if (value.decision === "false_positive" && !value.note?.trim()) {
      context.addIssue({ code: "custom", path: ["note"], message: "判定为误报时必须填写说明" });
    }
  });

export const samplingDecisionSchema = z
  .object({
    kind: z.literal("clearance_sample"),
    decision: z.enum(["clearance_confirmed", "missed_issue"]),
    note: z.string().max(2_000, "说明不能超过 2000 个字符").optional(),
  })
  .superRefine((value, context) => {
    if (value.note && controlCharacter.test(value.note)) {
      context.addIssue({ code: "custom", path: ["note"], message: "说明不能包含控制字符" });
    }
    if (value.decision === "missed_issue" && !value.note?.trim()) {
      context.addIssue({ code: "custom", path: ["note"], message: "发现漏放问题时必须填写说明" });
    }
  });

export function optionalNote(value: string): string | undefined {
  return value.length === 0 ? undefined : value;
}
