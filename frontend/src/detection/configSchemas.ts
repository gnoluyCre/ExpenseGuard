import { z } from "zod";

const exactText = z
  .string()
  .min(1)
  .max(512)
  .refine((value) => value === value.trim(), "exact 配置值不得包含首尾空白")
  .refine(
    (value) =>
      ![...value].some((character) => {
        const code = character.codePointAt(0) ?? 0;
        return code < 32 || code === 127;
      }),
    "exact 配置值不得包含控制字符",
  );
const decimal = z
  .string()
  .regex(/^\d+(?:\.\d+)?$/, "必须是正的非指数十进制字符串")
  .refine((value) => /[1-9]/.test(value), "十进制值必须大于零");
const common = {
  enabled: z.boolean(),
  min_eligible_rows: z.number().int().min(2).max(5000),
  min_eligible_rate_bps: z.number().int().min(1).max(10_000),
};

const splitInvoice = z
  .object({
    type: z.literal("split_invoice"),
    ...common,
    approval_thresholds: z.record(z.string().regex(/^[A-Z]{3}$/), decimal),
    currency_mode: z.enum(["field", "fixed"]),
    fixed_currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    aggregate_operator: z.enum(["gt", "gte"]),
    individual_floor_bps: z.number().int().min(1).max(9999),
    date_window_days: z.number().int().min(0).max(31),
    min_rows: z.number().int().min(2).max(50),
    merchant_aliases: z.record(exactText, exactText),
  })
  .strict()
  .refine((value) => (value.currency_mode === "fixed") === (value.fixed_currency !== null), {
    message: "fixed_currency 必须且只能用于 fixed currency mode",
  });

const sequentialInvoice = z
  .object({
    type: z.literal("sequential_invoice"),
    ...common,
    min_sequence_length: z.number().int().min(2).max(50),
    numeric_suffix_min_digits: z.number().int().min(1).max(32),
    numeric_suffix_max_digits: z.number().int().min(1).max(32),
    partition_fields: z.array(z.enum(["employee", "merchant", "invoice_type"])),
  })
  .strict()
  .refine((value) => new Set(value.partition_fields).size === value.partition_fields.length, {
    message: "partition fields 不得重复",
  })
  .refine((value) => value.numeric_suffix_max_digits >= value.numeric_suffix_min_digits, {
    message: "发票号后缀最大位数不能小于最小位数",
  });

const frequencyAnomaly = z
  .object({
    type: z.literal("frequency_anomaly"),
    ...common,
    period: z.enum(["calendar_week_monday", "calendar_month"]),
    min_population: z.number().int().min(3).max(5000),
    absolute_min_count: z.number().int().min(2).max(5000),
    mad_multiplier: decimal,
    mad_floor: decimal,
  })
  .strict();

const zonePair = z.tuple([exactText.max(64), exactText.max(64)]);
const spatiotemporal = z
  .object({
    type: z.literal("spatiotemporal_tier0"),
    ...common,
    location_aliases: z.record(exactText, exactText.max(64)),
    incompatible_zone_pairs: z.array(zonePair),
    min_zone_mapping_rate_bps: z.number().int().min(1).max(10_000),
  })
  .strict()
  .superRefine((value, context) => {
    const zones = new Set(Object.values(value.location_aliases));
    value.incompatible_zone_pairs.forEach(([left, right], index) => {
      if (left === right || !zones.has(left) || !zones.has(right)) {
        context.addIssue({
          code: "custom",
          path: ["incompatible_zone_pairs", index],
          message: "冲突分区必须不同且均由 exact location alias 引用",
        });
      }
    });
  });

export const detectionProfileSchema = z
  .object({
    schema_version: z.literal(1),
    algorithm_bundle_version: z.literal("correlation-v1"),
    detectors: z
      .array(
        z.discriminatedUnion("type", [
          splitInvoice,
          sequentialInvoice,
          frequencyAnomaly,
          spatiotemporal,
        ]),
      )
      .length(4),
  })
  .strict()
  .superRefine((value, context) => {
    const kinds = value.detectors.map((detector) => detector.type);
    if (new Set(kinds).size !== 4) {
      context.addIssue({
        code: "custom",
        path: ["detectors"],
        message: "四类 detector 必须各一项",
      });
    }
  });

export type ValidDetectionProfile = z.infer<typeof detectionProfileSchema>;
