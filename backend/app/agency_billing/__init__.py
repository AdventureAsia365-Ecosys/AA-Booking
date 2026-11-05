"""Agency billing (AA-1037 R4 slice 1, root ADR 0019 d9; bk-041 / bk-042): every invoice from AA to an agency, the
software fee (``saas``, AAS- numbers) and the tour settlement (``settlement``, AAT- numbers), drafted by two
``booking.job`` kinds, issued and paid by hand (maker-checker), and the billing status CIS reads.

- :mod:`app.agency_billing.config`   billing entity, tax rules, payment terms (data rows, edited in the staff admin)
- :mod:`app.agency_billing.invoices` list, detail, draft edits, issue (gap-free number, snapshots), void, e-invoice no
- :mod:`app.agency_billing.payments` record (maker), approve / reject (checker)
- :mod:`app.agency_billing.drafts`   the draft jobs ``bk_saas_invoice_draft`` / ``bk_settlement_draft`` + schedule
- :mod:`app.agency_billing.cis`      the CIS read ``catalog.v_tenant_billing_v1`` (subscriptions, bill-to)
"""
