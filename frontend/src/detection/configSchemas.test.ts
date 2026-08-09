import { describe, expect, it } from "vitest";

import { detectionProfileSchema } from "@/detection/configSchemas";

function profile() {
  return {
    schema_version: 1,
    algorithm_bundle_version: "correlation-v1",
    detectors: [
      {
        type: "split_invoice",
        enabled: true,
        min_eligible_rows: 2,
        min_eligible_rate_bps: 8000,
        approval_thresholds: { CNY: "1000" },
        currency_mode: "field",
        fixed_currency: null,
        aggregate_operator: "gte",
        individual_floor_bps: 8000,
        date_window_days: 3,
        min_rows: 2,
        merchant_aliases: {},
      },
      {
        type: "sequential_invoice",
        enabled: true,
        min_eligible_rows: 2,
        min_eligible_rate_bps: 8000,
        min_sequence_length: 3,
        numeric_suffix_min_digits: 3,
        numeric_suffix_max_digits: 12,
        partition_fields: ["employee", "merchant"],
      },
      {
        type: "frequency_anomaly",
        enabled: true,
        min_eligible_rows: 3,
        min_eligible_rate_bps: 8000,
        period: "calendar_month",
        min_population: 5,
        absolute_min_count: 5,
        mad_multiplier: "3",
        mad_floor: "1",
      },
      {
        type: "spatiotemporal_tier0",
        enabled: true,
        min_eligible_rows: 2,
        min_eligible_rate_bps: 8000,
        location_aliases: { 上海: "east", 北京: "north" },
        incompatible_zone_pairs: [["east", "north"]],
        min_zone_mapping_rate_bps: 8000,
      },
    ],
  };
}

describe("detectionProfileSchema", () => {
  it("接受恰好四类合法 detector", () => {
    expect(detectionProfileSchema.safeParse(profile()).success).toBe(true);
  });

  it("拒绝会改变 exact 语义的空白、控制字符和未知 zone", () => {
    const value = profile();
    const spatial = value.detectors[3] as {
      location_aliases: Record<string, string>;
      incompatible_zone_pairs: string[][];
    };
    spatial.location_aliases = { " 上海": "east", 北京: "north\n" };
    spatial.incompatible_zone_pairs = [["east", "unknown"]];
    expect(detectionProfileSchema.safeParse(value).success).toBe(false);
  });

  it("拒绝零 Decimal、缺 detector 和重复 partition field", () => {
    const value = profile();
    const frequency = value.detectors[2] as { mad_floor: string };
    frequency.mad_floor = "0.00";
    const sequential = value.detectors[1] as { partition_fields: string[] };
    sequential.partition_fields = ["employee", "employee"];
    value.detectors.pop();
    expect(detectionProfileSchema.safeParse(value).success).toBe(false);
  });
});
