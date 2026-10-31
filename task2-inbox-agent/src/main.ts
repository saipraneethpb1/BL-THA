import { claudeMissing } from "./llm/claude.js";
import { buildApp, defaultDeps } from "./server.js";

const deps = defaultDeps();
const app = buildApp(deps);
await app.listen({ port: deps.config.port, host: "0.0.0.0" });
console.log(
  `Inbox Agent on http://localhost:${deps.config.port} (drafting ${deps.agent ? "on" : `off: ${claudeMissing()}`}, Knowledge Brain at ${deps.config.kbUrl})`,
);
