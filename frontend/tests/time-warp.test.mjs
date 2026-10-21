// Local video stretch: the clock map the preview and the editor share with the render
// (backend dubbing/time_warp.py). Run: node --test frontend/tests
import assert from "node:assert/strict";
import { test } from "node:test";
import { toOutputTime, warpFactorAt } from "../src/util.ts";

const warp = [
  { start: 10, length: 2, factor: 1.25 }, // +0.5s
  { start: 20, length: 4, factor: 1.1 }, // +0.4s
];

test("no stretch is the identity", () => {
  assert.equal(toOutputTime([], 12.3), 12.3);
  assert.equal(warpFactorAt([], 12.3), 1);
});

test("output time grows inside a segment and shifts everything after it", () => {
  assert.equal(toOutputTime(warp, 5), 5);
  assert.equal(toOutputTime(warp, 10), 10);
  assert.ok(Math.abs(toOutputTime(warp, 11) - 11.25) < 1e-9);
  assert.ok(Math.abs(toOutputTime(warp, 15) - 15.5) < 1e-9);
  assert.ok(Math.abs(toOutputTime(warp, 30) - 30.9) < 1e-9);
});

test("the picture is slowed only inside a segment", () => {
  assert.equal(warpFactorAt(warp, 9.99), 1);
  assert.equal(warpFactorAt(warp, 10), 1.25);
  assert.equal(warpFactorAt(warp, 12), 1);
  assert.equal(warpFactorAt(warp, 23.9), 1.1);
});
