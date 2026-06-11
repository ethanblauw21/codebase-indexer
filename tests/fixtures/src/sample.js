// Sample JavaScript module for golden snapshot testing.
import { join } from "path";

class Logger {
  constructor(prefix) {
    this.prefix = prefix;
  }

  log(message) {
    console.log(`[${this.prefix}] ${message}`);
  }
}

const createLogger = (prefix) => new Logger(prefix);

export { Logger, createLogger };
