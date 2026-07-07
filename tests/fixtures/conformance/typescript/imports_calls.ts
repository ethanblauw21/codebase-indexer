// Feature: import forms (named / default) plus calls into imported symbols.

import { readFile } from "fs";
import path from "path";

export function loadConfig(dir: string): string {
  const full = path.join(dir, "config.json");
  readFile(full, () => {});
  return full;
}
