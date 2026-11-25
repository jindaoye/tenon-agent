import { afterEach, describe, expect, it, vi } from "vitest";
import { userMessageContent } from "./chat";
import { sessionFileContentUrl, uploadSessionFiles } from "../projects/files";
import { windowOperationControl } from "../transport/operationControl";

afterEach(() => vi.restoreAllMocks());

describe("image input", () => {
  it("separates visual content from ordinary file references", () => {
    const image = { source: "upload" as const, path: "asset:asset_0123456789abcdef0123456789abcdef", display_path: "photo.png" };
    const file = { source: "workspace" as const, path: "workspace:notes.txt", display_path: "workspace:notes.txt" };
    expect(userMessageContent("compare", [file, image])).toEqual([
      { type: "text", text: "compare", references: [file] },
      { type: "image", asset_id: image.path.slice(6) },
    ]);
    expect(sessionFileContentUrl("session", image.source, image.path)).toBe(
      "/api/sessions/session/assets/asset_0123456789abcdef0123456789abcdef/content",
    );
  });
  it("uploads images as assets while keeping ordinary file batches", async () => {
    const send = vi.spyOn(windowOperationControl, "requestUsing")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        asset_id: "asset_0123456789abcdef0123456789abcdef", filename: "photo.png", bytes: 10, mime_type: "image/png",
      }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([
        { source: "upload", path: "workspace:uploads/a.txt" },
        { source: "upload", path: "workspace:uploads/b.txt" },
      ])));
    const results = await uploadSessionFiles("session", [
      new File(["image"], "photo.png", { type: "image/png" }),
      new File(["a"], "a.txt"), new File(["b"], "b.txt"),
    ]);
    expect(send.mock.calls.map((args) => args[0])).toEqual([
      "/api/sessions/session/assets", "/api/sessions/session/files",
    ]);
    expect((send.mock.calls[1][1].body as FormData).getAll("files")).toHaveLength(2);
    expect(results.map((item) => item.path)).toEqual([
      "asset:asset_0123456789abcdef0123456789abcdef", "workspace:uploads/a.txt", "workspace:uploads/b.txt",
    ]);
  });
});
