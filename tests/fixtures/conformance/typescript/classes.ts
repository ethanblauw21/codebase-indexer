// Feature: a class with a constructor and methods (owns edges, method kinds).

export class Counter {
  private value: number;

  constructor(start: number) {
    this.value = start;
  }

  increment(): number {
    this.value += 1;
    return this.value;
  }

  reset(): void {
    this.value = 0;
  }
}
