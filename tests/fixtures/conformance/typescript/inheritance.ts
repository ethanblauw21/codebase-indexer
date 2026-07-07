// Feature: `extends` and `implements` relationships between types.

export interface Speaker {
  speak(): string;
}

export class Animal {
  speak(): string {
    return "...";
  }
}

export class Dog extends Animal implements Speaker {
  speak(): string {
    return "woof";
  }
}
