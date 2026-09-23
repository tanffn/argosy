import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DiscordAdvisorStatus } from "../DiscordAdvisorStatus";

const state = vi.hoisted(() => ({ data: null as null | { status: string; message: string; attention_required: boolean }, failed: false, retry: vi.fn() }));
vi.mock("../use-home-read", () => ({ useHomeRead: () => state }));

afterEach(() => { cleanup(); state.data = null; state.failed = false; });

describe("private advisor connection status", () => {
  it("does not label an unconfigured bot an urgent failure", () => {
    state.data = { status: "unconfigured", message: "Not configured. Private server setup is pending.", attention_required: false };
    render(<DiscordAdvisorStatus userId="owner" />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Not configured");
  });
  it("surfaces authentication repair as a real error", () => {
    state.data = { status: "auth_error", message: "Discord rejected the bot token. Replace it locally and restart.", attention_required: true };
    render(<DiscordAdvisorStatus userId="owner" />);
    expect(screen.getByRole("alert").textContent).toContain("Replace it locally");
  });
  it("does not retain an apparent healthy connection after a failed refresh", () => {
    state.failed = true;
    render(<DiscordAdvisorStatus userId="owner" />);
    expect(screen.getByRole("status").textContent).toContain("not evidence the bot is connected");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
