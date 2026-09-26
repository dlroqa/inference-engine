// Minimal typings for the Node built-ins the wiring-docs drift test uses
// (Vitest runs tests in Node). The dashboard does not depend on @types/node;
// these declare only what that test calls.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function writeFileSync(path: string, data: string, encoding: "utf8"): void;
}

declare module "node:url" {
  export function fileURLToPath(url: URL | string): string;
}
