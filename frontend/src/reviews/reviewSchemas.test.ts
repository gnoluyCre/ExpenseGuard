import { describe, expect, it } from "vitest";

import {
  findingDecisionSchema,
  optionalNote,
  samplingConfigSchema,
  samplingDecisionSchema,
} from "@/reviews/reviewSchemas";

describe("samplingConfigSchema", () => {
  it("接受整数边界与正常 Unicode", () => {
    expect(
      samplingConfigSchema.parse({
        expected_current_version: 0,
        rate_bps: 1,
        min_sample_size: 1,
        max_sample_size: 1,
        change_reason: "首批抽样参数",
      }),
    ).toMatchObject({ rate_bps: 1, max_sample_size: 1 });
    expect(
      samplingConfigSchema.safeParse({
        expected_current_version: 9,
        rate_bps: 10_000,
        min_sample_size: 2,
        max_sample_size: 20,
        change_reason: "边界值",
      }).success,
    ).toBe(true);
  });

  it.each([
    { rate_bps: 0, min_sample_size: 1, max_sample_size: 1, change_reason: "x" },
    { rate_bps: 10_001, min_sample_size: 1, max_sample_size: 1, change_reason: "x" },
    { rate_bps: 1.5, min_sample_size: 1, max_sample_size: 1, change_reason: "x" },
    { rate_bps: 100, min_sample_size: 2, max_sample_size: 1, change_reason: "x" },
    { rate_bps: 100, min_sample_size: 1, max_sample_size: 1, change_reason: "" },
    { rate_bps: 100, min_sample_size: 1, max_sample_size: 1, change_reason: "含\n换行" },
  ])("拒绝非法配置 %#", (value) => {
    expect(samplingConfigSchema.safeParse({ expected_current_version: 0, ...value }).success).toBe(
      false,
    );
  });
});

describe("decision schemas", () => {
  it("finding 只在 false_positive 时要求说明", () => {
    expect(
      findingDecisionSchema.safeParse({ kind: "finding", decision: "confirmed" }).success,
    ).toBe(true);
    expect(
      findingDecisionSchema.safeParse({ kind: "finding", decision: "false_positive" }).success,
    ).toBe(false);
    expect(
      findingDecisionSchema.safeParse({
        kind: "finding",
        decision: "false_positive",
        note: "   ",
      }).success,
    ).toBe(false);
    expect(
      findingDecisionSchema.safeParse({
        kind: "finding",
        decision: "false_positive",
        note: "重复规则导致误报",
      }).success,
    ).toBe(true);
  });

  it("sample 只在 missed_issue 时要求说明，并保留 Unicode 原文", () => {
    expect(
      samplingDecisionSchema.safeParse({
        kind: "clearance_sample",
        decision: "clearance_confirmed",
      }).success,
    ).toBe(true);
    expect(
      samplingDecisionSchema.safeParse({
        kind: "clearance_sample",
        decision: "missed_issue",
      }).success,
    ).toBe(false);
    expect(
      samplingDecisionSchema.safeParse({
        kind: "clearance_sample",
        decision: "missed_issue",
        note: "　　",
      }).success,
    ).toBe(false);
    expect(
      samplingDecisionSchema.parse({
        kind: "clearance_sample",
        decision: "missed_issue",
        note: "抬头异常　保留全角空格",
      }).note,
    ).toBe("抬头异常　保留全角空格");
  });

  it("拒绝控制字符和超长说明", () => {
    expect(
      findingDecisionSchema.safeParse({
        kind: "finding",
        decision: "confirmed",
        note: "x\u0000",
      }).success,
    ).toBe(false);
    expect(
      samplingDecisionSchema.safeParse({
        kind: "clearance_sample",
        decision: "missed_issue",
        note: "x".repeat(2_001),
      }).success,
    ).toBe(false);
  });

  it("只把真正空字符串转成未提供，不 trim 人工原文", () => {
    expect(optionalNote("")).toBeUndefined();
    expect(optionalNote("   ")).toBe("   ");
  });
});
