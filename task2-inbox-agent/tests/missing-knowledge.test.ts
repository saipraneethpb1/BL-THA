import { expect, test } from "vitest";
import { autoSendBlocker } from "../src/feedback/autonomy.js";

test("unresolved missing information blocks sending even without a knowledge gap", () => {
  const blocker = autoSendBlocker(
    { status: "autonomous", streak: 100 } as Parameters<typeof autoSendBlocker>[0],
    { category: "how_to", urgency: "normal", sentiment: "neutral", eligible: true,
      hasBlockingViolation: false, secretsFound: false, suspectedInjection: false,
      knowledgeGap: false, missingKnowledge: true } as Parameters<typeof autoSendBlocker>[1],
  );
  expect(blocker).toBe("knowledge evidence contains unresolved missing information");
});

