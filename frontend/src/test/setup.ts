import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom 不会在测试之间自动清空 document。不清的话上一个测试渲染的节点
// 还留在 DOM 里，`getByRole` 会撞上「找到多个」而失败——或者更糟，
// 断言碰巧命中上一个测试的残留节点，于是本该失败的测试变绿。
afterEach(() => {
  cleanup();
});
