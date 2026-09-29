// ADR-044 resolution fixture: calls bound through this file's imports.
import { format } from "date-fns";
import * as tools from "./tb/tools";
import { load } from "./tb/store";
import { fromBarrel } from "./tb";

export function stamp(d: Date): string {
  return format(d, "yyyy");
}

export function build(): string {
  return tools.make();
}

export function fetch(key: string): string {
  return load(key);
}

export function viaBarrel(): number {
  return fromBarrel();
}

export function keep(repo: any): void {
  repo.save();
}
