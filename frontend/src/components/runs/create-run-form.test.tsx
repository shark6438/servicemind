import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CreateRunForm } from "@/components/runs/create-run-form";

const fx = vi.hoisted(() => ({
  request: vi.fn(), push: vi.fn(), roles: new Set(["analyst"]), getToken: vi.fn(async () => "test-token"),
}));
vi.mock("@/lib/api", () => ({ apiRequest: fx.request }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: fx.push }) }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => fx }));

beforeEach(() => {
  fx.request.mockReset(); fx.push.mockReset(); fx.roles = new Set(["analyst"]);
});
afterEach(cleanup);

function readyForm() {
  const view = render(<CreateRunForm />);
  fireEvent.change(screen.getByRole("spinbutton", { name: "关联 GLPI 工单" }), { target: { value: "42" } });
  fireEvent.change(screen.getByRole("textbox", { name: "你的问题" }), { target: { value: "分析 VPN 故障" } });
  return view.container.querySelector("form")!;
}

function requestKey(index: number) {
  return new Headers((fx.request.mock.calls[index][3] as RequestInit).headers).get("Idempotency-Key");
}

describe("run submission intent", () => {
  it("reuses the key after a lost response and keeps successful navigation locked", async () => {
    fx.request.mockRejectedValueOnce(new TypeError("网络已断开")).mockResolvedValueOnce({ id: "accepted-run" });
    const form = readyForm();
    fireEvent.submit(form);
    await screen.findByRole("alert");
    const firstKey = requestKey(0);
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/);
    fireEvent.submit(form);
    await waitFor(() => expect(fx.push).toHaveBeenCalledWith("/runs/accepted-run"));
    expect(requestKey(1)).toBe(firstKey);
    expect(fx.request.mock.calls[1][3].body).toBe(fx.request.mock.calls[0][3].body);
    fireEvent.submit(form);
    expect(fx.request).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "正在发送" })).toBeDisabled();
  });

  it("uses a new key when the submitted request changes", async () => {
    fx.request.mockRejectedValueOnce(new Error("暂时不可用")).mockResolvedValueOnce({ id: "new-run" });
    const form = readyForm();
    fireEvent.submit(form);
    await screen.findByRole("alert");
    fireEvent.change(screen.getByRole("textbox", { name: "你的问题" }), { target: { value: "查询新的 VPN 工单" } });
    fireEvent.submit(form);
    await waitFor(() => expect(fx.push).toHaveBeenCalled());
    expect(requestKey(1)).not.toBe(requestKey(0));
  });

  it("prevents same-tick duplicate submits before React updates the pending button", () => {
    fx.request.mockReturnValue(new Promise(() => {}));
    const form = readyForm();
    fireEvent.submit(form); fireEvent.submit(form);
    expect(fx.request).toHaveBeenCalledTimes(1);
  });

  it("does not submit when the current identity lacks the analyst role", () => {
    fx.roles = new Set(["viewer"]);
    const form = readyForm();
    fireEvent.submit(form);
    expect(fx.request).not.toHaveBeenCalled();
  });
});
