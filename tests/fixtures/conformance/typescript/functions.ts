// Feature: function declarations and the call edges between them.

export function greet(name: string): string {
  return formatName(name);
}

function formatName(name: string): string {
  return name.trim();
}

function main(): void {
  greet("ada");
  formatName("grace");
}
