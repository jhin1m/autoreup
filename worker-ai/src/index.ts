type Env = {
  AI: Ai;
};

type WhisperResult = {
  text?: string;
  segments?: Array<{
    start?: number;
    end?: number;
    text?: string;
  }>;
  words?: Array<{
    start?: number;
    end?: number;
    word?: string;
    text?: string;
  }>;
};

type TranslateResult = {
  translated_text?: string;
  translation?: string;
  text?: string;
  result?: string;
};

type ChatResult = {
  response?: string;
  choices?: Array<{ message?: { content?: string }; text?: string }>;
};

const ASR_MODELS = new Set(["@cf/openai/whisper", "@cf/openai/whisper-large-v3-turbo"]);

type TranslateBatchItem = {
  id: number;
  text: string;
};

function extractJsonArray(text: string): Array<{ id: number; translated: string }> {
  const trimmed = text.trim();
  const start = trimmed.indexOf("[");
  if (start < 0) {
    throw new Error("LLM response does not contain JSON array");
  }
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < trimmed.length; i++) {
    const char = trimmed[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (char === "[") depth++;
    if (char === "]") depth--;
    if (depth === 0) {
      return JSON.parse(trimmed.slice(start, i + 1)) as Array<{ id: number; translated: string }>;
    }
  }
  throw new Error("LLM JSON array is not closed");
}

function parseTranslations(text: string): Array<{ id: number; translated: string }> {
  try {
    return extractJsonArray(text);
  } catch (_error) {
    const rows: Array<{ id: number; translated: string }> = [];
    for (const line of text.split("\n")) {
      const match = line.trim().match(/^(\d+)\s*(?:\t|\||:|：|-|—)\s*(.+)$/);
      if (match) {
        rows.push({ id: Number(match[1]), translated: match[2].trim().replace(/^"|"$/g, "") });
      }
    }
    if (!rows.length) throw _error;
    return rows;
  }
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

function base64ToBytes(base64: string): number[] {
  const binary = atob(base64);
  const bytes = new Array<number>(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

export default {
  async fetch(request, env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return json({ ok: true });
    }

    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/translate") {
      const body = await request.json<{ text?: string; source_lang?: string; target_lang?: string }>();
      if (!body.text) {
        return json({ error: "Missing text" }, 400);
      }

      try {
        const result = await env.AI.run("@cf/meta/m2m100-1.2b", {
          text: body.text,
          source_lang: body.source_lang || "chinese",
          target_lang: body.target_lang || "vietnamese",
        }) as TranslateResult;
        return json({ translated: result.translated_text || result.translation || result.text || result.result || "", raw: result });
      } catch (error) {
        console.error(JSON.stringify({ error: String(error), route: "translate" }));
        return json({ error: String(error) }, 502);
      }
    }

    if (request.method === "POST" && url.pathname === "/translate-batch") {
      const body = await request.json<{
        items?: TranslateBatchItem[];
        previous_context?: TranslateBatchItem[];
        next_context?: TranslateBatchItem[];
        model?: string;
      }>();
      const items = body.items || [];
      if (!items.length) {
        return json({ error: "Missing items" }, 400);
      }

      const system = [
        "Bạn là biên dịch phụ đề tiếng Trung sang tiếng Việt.",
        "Ngữ cảnh: video kể chuyện phim/truyện/anime Trung Quốc.",
        "Dịch tự nhiên, đúng nghĩa, văn nói, ngắn gọn để hiển thị phụ đề một dòng.",
        "Không dịch máy từng chữ. Không thêm giải thích.",
        "Giữ nguyên id. Không gộp, không tách, không đổi thứ tự.",
        "Mỗi dòng output có dạng: id<TAB>bản dịch tiếng Việt",
        "Không markdown. Không JSON. Không ghi chú.",
      ].join("\n");

      const prompt = JSON.stringify({
        previous_context: body.previous_context || [],
        translate_items: items,
        next_context: body.next_context || [],
      }, null, 2);

      try {
        const result = await env.AI.run(body.model || "@cf/qwen/qwen3-30b-a3b-fp8", {
          prompt: `${system}\n\nINPUT JSON:\n${prompt}\n\nOUTPUT TSV ONLY:`,
          temperature: 0.2,
          max_tokens: 2048,
        }) as ChatResult;
        const responseText = result.response || result.choices?.[0]?.message?.content || result.choices?.[0]?.text || "";
        const translated = parseTranslations(responseText);
        return json({ items: translated, raw: responseText });
      } catch (error) {
        console.error(JSON.stringify({ error: String(error), route: "translate-batch" }));
        return json({ error: String(error) }, 502);
      }
    }

    if (request.method !== "POST" || url.pathname !== "/transcribe") {
      return json({ error: "POST /transcribe, /translate, or /translate-batch only" }, 404);
    }

    const contentType = request.headers.get("content-type") || "";
    let audio: string | undefined;
    let language = "zh";
    let offset = 0;
    let model = "@cf/openai/whisper-large-v3-turbo";

    if (contentType.includes("application/json")) {
      const body = await request.json<{ audio?: string; language?: string; offset?: number; model?: string }>();
      audio = body.audio;
      language = body.language || language;
      offset = body.offset || 0;
      model = body.model || model;
    } else {
      const bytes = await request.arrayBuffer();
      audio = arrayBufferToBase64(bytes);
      language = request.headers.get("x-language") || language;
      offset = Number(request.headers.get("x-offset") || "0");
      model = request.headers.get("x-model") || model;
    }

    if (!audio) {
      return json({ error: "Missing audio" }, 400);
    }
    if (!ASR_MODELS.has(model)) {
      return json({ error: `Unsupported ASR model: ${model}` }, 400);
    }

    let result: WhisperResult;
    try {
      const input = model === "@cf/openai/whisper"
        ? { audio: base64ToBytes(audio) }
        : {
            audio,
            language,
            task: "transcribe",
            vad_filter: true,
            condition_on_previous_text: false,
          };
      result = await env.AI.run(model, input) as WhisperResult;
    } catch (error) {
      console.error(JSON.stringify({ error: String(error), offset }));
      return json({ error: String(error), offset, model }, 502);
    }

    const rawSegments = result.segments?.length ? result.segments : wordsToSegments(result.words || [], result.text || "");
    const segments = rawSegments.map((segment) => ({
      start: (segment.start || 0) + offset,
      end: (segment.end || 0) + offset,
      text: segment.text || "",
    }));
    // Word-level timestamps (when the model provides them) let the client snap
    // cue boundaries to when each word is actually spoken, instead of estimating.
    const words = (result.words || [])
      .map((word) => ({
        start: (word.start || 0) + offset,
        end: (word.end || 0) + offset,
        word: (word.word || word.text || "").trim(),
      }))
      .filter((word) => word.word);

    return json({ text: result.text || "", offset, model, segments, words });
  },
} satisfies ExportedHandler<Env>;

function wordsToSegments(words: NonNullable<WhisperResult["words"]>, fallbackText: string): NonNullable<WhisperResult["segments"]> {
  if (!words.length) {
    return fallbackText ? [{ start: 0, end: 0, text: fallbackText }] : [];
  }
  const text = words.map((word) => word.word || word.text || "").join("").trim();
  return [{ start: words[0].start || 0, end: words[words.length - 1].end || 0, text }];
}
