import type { KnowledgeAnswer, KnowledgeClient } from "../src/knowledge-client/client.js";
import { KnowledgeUnavailableError } from "../src/knowledge-client/client.js";
import type {
  Classification,
  Classifier,
  DraftOutput,
  DraftRequest,
  Drafter,
  EmailForModel,
  Usage,
} from "../src/llm/models.js";
import { InboxStore } from "../src/store/db.js";

const usage = (model: string): Usage => ({
  model, promptVersion: "test_v1", inputTokens: 100, outputTokens: 50, costUsd: 0.001,
});

export function classification(over: Partial<Classification> = {}): Classification {
  return {
    category: "how_to", secondary: [], urgency: "normal", language: "en",
    summary: "Asks how to do something", questions: ["How do I change the logo?"],
    sentiment: "neutral", suspected_injection: false, ...over,
  };
}

export class FakeClassifier implements Classifier {
  seen: EmailForModel[] = [];
  constructor(public result: Classification = classification()) {}
  async classify(email: EmailForModel) {
    this.seen.push(email);
    return { result: this.result, usage: usage("fake-classify") };
  }
}

export class FakeDrafter implements Drafter {
  seen: DraftRequest[] = [];
  constructor(
    public result: DraftOutput = {
      subject: "Re: Logo",
      body: "Hi Megan,\n\nGo to Design Settings and upload the new logo.\n\nThe Lakeview team",
      used_evidence: [1],
      follow_up_in_days: null,
      note_for_reviewer: "",
    },
  ) {}
  async draft(req: DraftRequest) {
    this.seen.push(req);
    return { result: this.result, usage: usage("fake-draft") };
  }
}

export class FakeKnowledge implements KnowledgeClient {
  down = false;
  status: KnowledgeAnswer["status"] = "answered";
  questions: string[] = [];
  async query(question: string): Promise<KnowledgeAnswer> {
    this.questions.push(question);
    if (this.down) throw new KnowledgeUnavailableError("down");
    return {
      status: this.status, answer: "Upload it under Design Settings.", needs_human: this.status !== "answered",
      sources: [{ title: "Logo", url: "https://help.test/1", source_type: "article", section: null, score: 0.8, image_urls: [] }],
      escalation_id: null, missing_info: null, cost_usd: 0.002, latency_ms: 900,
    };
  }
}

let n = 0;
export function addEmail(store: InboxStore, body = "How do I change my logo?", over: Partial<{ id: string; subject: string }> = {}): string {
  const id = over.id ?? `T${++n}`;
  store.addEmail({
    id, from_name: "Megan", from_email: "megan@example.com", subject: over.subject ?? "Logo",
    body, thread: [], received_at: "2026-10-17T10:00:00Z",
  });
  return id;
}
