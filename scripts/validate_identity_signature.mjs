#!/usr/bin/env node
import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import fs from "node:fs";

const contract = JSON.parse(
  fs.readFileSync("tests/contract/oac_irs/identity/v1/contract.json", "utf8"),
);
const vector = contract.test_vector;
const bodyHash = createHash("sha256").update(vector.request.body, "utf8").digest("hex");
if (bodyHash !== vector.content_sha256) throw new Error("body SHA-256 mismatch");
const actual = `v1=${createHmac("sha256", vector.secret).update(vector.canonical, "utf8").digest("hex")}`;
if (!timingSafeEqual(Buffer.from(actual), Buffer.from(vector.signature))) {
  throw new Error("Node HMAC vector mismatch");
}
process.stdout.write("Node HMAC 向量检查通过\n");
