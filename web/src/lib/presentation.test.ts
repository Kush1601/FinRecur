import { describe, expect, it } from "vitest";
import {
  describeLedgerEvent,
  displayCounterparty,
  formatSellerLocations,
  humanizeCode,
  shortReference,
} from "./presentation";

describe("presentation helpers", () => {
  it("uses product language for internal reason and fix codes", () => {
    expect(humanizeCode("suspected_duplicate")).toBe("Possible duplicate payment");
    expect(humanizeCode("record_fee_deduction")).toBe("Record processing fee");
  });

  it("keeps identifiers useful without exposing the entire UUID", () => {
    expect(shortReference("24b24dab-ecfa-549d-bb1a-96d1258fd745")).toBe("24b24dab…");
  });

  it("makes generated customer labels visibly anonymous", () => {
    expect(displayCounterparty("Customer 0da12f")).toBe("Customer •••0da12f");
  });

  it("formats a seller location without exposing its source id", () => {
    expect(formatSellerLocations([{ city: "sao_paulo", state: "sp" }])).toBe("Sao Paulo, SP");
    expect(formatSellerLocations([
      { city: "campinas", state: "SP" },
      { city: "curitiba", state: "PR" },
    ])).toBe("2 seller locations");
  });

  it("summarizes a dry run without exposing its snapshot hash", () => {
    expect(
      describeLedgerEvent({
        action: "dry_run",
        after: {
          predicted: {
            matched: 11,
            applied_centavos: 507_412,
            written_off_centavos: 9_892,
          },
          side_effect_count: 0,
          snapshot_hash: "hidden-from-primary-view",
        },
      }),
    ).toEqual({
      title: "Dry run completed",
      summary: "11 receipts would match · R$ 5.074,12 affected · R$ 98,92 adjustment · No side effects",
    });
  });
});
