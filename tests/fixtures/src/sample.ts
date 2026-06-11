// Sample TypeScript module for golden snapshot testing.
import { readFileSync } from "fs";

export interface Record {
  id: string;
  value: number;
}

export type ProcessFn = (r: Record) => string;

export class DataService {
  private records: Record[] = [];

  constructor(private readonly path: string) {}

  load(): void {
    const raw = readFileSync(this.path, "utf-8");
    this.records = JSON.parse(raw);
  }

  process(): string[] {
    return this.records.map((r) => `${r.id}:${r.value}`);
  }
}

export const formatRecord: ProcessFn = (r) => `${r.id}=${r.value}`;
