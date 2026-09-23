import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type E2EOrderLineDTO, type FillItem, type ManualFillResponse } from "@/lib/api";
import { ManualFillForm } from "../ManualFillForm";

const line = { proposal: { id: 7, target_quantity: 2, account_id: "Leumi" },
  execution: { filled_quantity: 0, broker_order_id: null } } as E2EOrderLineDTO;
const receipt: FillItem = { id: 4, user_id: "test", proposal_id: 7, broker: "leumi_tsv",
  broker_order_id: "order", external_fill_id: "execution", account_id: "Leumi", ticker: "X", action: "buy",
  quantity: 2, price: 100, commission: null, filled_at: "2026-09-22T14:00:00+00:00", paper: false,
  execution_time_confirmed: false, commission_confirmed: false, price_currency: null, commission_currency: null,
  native_account_id: null, book_status: "needs_settlement", book_reason: "Settlement facts required",
  applied_snapshot_id: null, settlement: null };
const response = { fill_id: 4, created: false, book_status: "needs_reconciliation",
  book_reason: "No matching account cash", applied_snapshot_id: null } as ManualFillResponse;
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
  vi.spyOn(api, "fillsList").mockResolvedValue({ rows: [], total: 0 });
  vi.spyOn(api, "proposalManualFill").mockResolvedValue(response);
});
afterEach(() => vi.restoreAllMocks());

describe("broker settlement capture", () => {
  it("requires explicit units, fees, withholding, cash and offset; sends broker facts without inference", async () => {
    render(<ManualFillForm line={line} userId="test" onSaved={vi.fn().mockResolvedValue(undefined)} />);
    change("Broker order ID", "order"); change("Execution ID", "execution"); change("Fill price", "100");
    fireEvent.click(screen.getByRole("checkbox"));
    const save = screen.getByRole("button", { name: "Record verified fill" });
    expect(save).toBeDisabled();
    change("Settlement currency", "NIS"); change("Tax withheld", "0"); change("Commission", "0");
    change("Net cash change", "-200"); change("Settlement reference", "statement-12");
    change("Execution timestamp with UTC offset", "2026-09-22T16:31:00");
    expect(save).toBeDisabled();
    change("Execution timestamp with UTC offset", "2026-09-22T16:31:00+03:00");
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    expect(await screen.findByText(/Receipt saved; portfolio not updated/)).toHaveTextContent("No matching account cash");
    expect(api.proposalManualFill).toHaveBeenCalledWith(7, expect.objectContaining({
      filled_at: "2026-09-22T16:31:00+03:00", commission: 0,
      settlement: { currency: "NIS", tax_withheld: "0", net_cash_delta: "-200", reference: "statement-12", listing_symbol: null },
    }));
  });

  it("reopens the same execution to complete missing details, keeping identity fixed", async () => {
    vi.mocked(api.fillsList).mockResolvedValue({ rows: [receipt], total: 1 });
    render(<ManualFillForm line={line} userId="test" onSaved={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Complete receipt details" }));
    expect(screen.getByLabelText("Execution ID")).toHaveValue("execution");
    for (const label of ["Broker order ID", "Execution ID", "Filled quantity", "Fill price"]) expect(screen.getByLabelText(label)).toBeDisabled();
    expect(screen.getByLabelText("Execution timestamp with UTC offset")).toHaveValue("");
    change("Commission", "1");
    fireEvent.click(screen.getByRole("button", { name: "Save receipt details" }));
    await waitFor(() => expect(api.proposalManualFill).toHaveBeenCalledWith(7, expect.objectContaining({
      external_fill_id: "execution", quantity: 2, price: 100, commission: 1, filled_at: null, settlement: null,
    })));
  });

  it("shows successful book application without claiming tax or statement verification", async () => {
    vi.mocked(api.proposalManualFill).mockResolvedValue({ ...response, book_status: "applied", applied_snapshot_id: 88 });
    const onSaved = vi.fn().mockRejectedValue(new Error("refresh failed"));
    render(<ManualFillForm line={line} userId="test" onSaved={onSaved} />);
    change("Broker order ID", "order"); change("Execution ID", "execution"); change("Fill price", "100");
    const save = screen.getByRole("button", { name: "Record verified fill" });
    await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
    expect(await screen.findByText(/Shares and cash updated in snapshot #88/)).toHaveTextContent("Tax-lot and statement verification remain separate");
    expect(await screen.findByRole("alert")).toHaveTextContent("Receipt saved, but the trade-plan display could not refresh");
  });

  it("keeps applied receipts read-only and exposes receipt-load errors", async () => {
    vi.mocked(api.fillsList).mockRejectedValueOnce(new Error("offline"));
    render(<ManualFillForm line={line} userId="test" onSaved={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load saved receipts");
    vi.mocked(api.fillsList).mockResolvedValue({ rows: [{ ...receipt, book_status: "applied", applied_snapshot_id: 8 }], total: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Retry receipts" }));
    fireEvent.click(await screen.findByRole("button", { name: "View receipt" }));
    expect(screen.queryByRole("button", { name: "Save receipt details" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Commission")).toBeDisabled();
  });
});
