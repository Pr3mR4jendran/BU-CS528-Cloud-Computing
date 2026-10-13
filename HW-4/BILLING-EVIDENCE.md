# Billing screenshots and exports for the HW4 report

The report's implementation, stress results and automatic cleanup are verified. The actual billed cost still needs the evidence below. All measured deployment activity took place on October 13, 2026: deployment state was created at 06:28:26 UTC (02:28:26 EDT), the final stress suite ran 15:56:36-16:09:22 UTC (11:56:36-12:09:22 EDT), and resource absence was verified at 16:40:19 UTC (12:40:19 EDT). Infrastructure was deleted during the first cleanup; the final verification retry did not recreate it.

1. Open Google Cloud Console, select `bu-cs528-prem-project`, then **Billing > Reports** for its linked billing account.
2. Set **Time range** to **Charge period** (formerly **Usage date**) with a custom range of **October 13, 2026 through October 13, 2026**. Set the **Projects** filter to `bu-cs528-prem-project`. Leave services unrestricted for the overall cost view. Preserve credits/savings in the net-cost view and show the currency.
3. Set **Group by: Service**. Capture the visible date/project filters, chart/total, and full service table. Include usage cost, credits/savings, and subtotal. Save screenshots as `screenshots/billing-service-total.png` and `screenshots/billing-service-table.png`. Two captures are fine when the table does not fit.
4. Download the filtered table with **Download CSV** above the summary table. Save it under `evidence/billing/` as `billing-by-service-2026-10-13.csv`.
5. Switch to **Group by: SKU**. Capture detailed rows for Compute Engine VM usage, persistent disks, external IPv4, network transfer, Cloud Storage, Pub/Sub and Logging where present. Keep the project/date filters visible in at least one image and include the usage units/amounts, charges, credits and subtotal. Save `screenshots/billing-sku-details-1.png` (and additional numbered images if needed). Download `evidence/billing/billing-by-sku-2026-10-13.csv`.
6. If the label filter is available, add `assignment=cs528-hw4` and capture an additional labeled view as `screenshots/billing-hw4-label.png`, along with its CSV. This can attribute labeled VM/disk/Pub/Sub usage, but may omit unlabeled or shared charges. Keep the unfiltered project/service and SKU views too. Earlier homework resources and the existing dataset share this project, so a project-day total is not automatically a stress-test-only cost.
7. Record when each report was captured, the billing account currency, usage cost before credits, total credits/savings and net subtotal. Account-level invoice taxes are not a per-project usage total. No payment-card details are needed.

Google says cost details are typically available within a day but sometimes take more than 24 hours. If the October 13 view is zero or incomplete today, capture it as provisional and refresh after data settles; do not conclude the assignment cost zero from an empty current-day view. A rounded `$0.00` can also conceal a small amount: the CSV includes an unrounded subtotal.

Once the screenshots and CSVs are saved, the cost section can be updated with a supported amount and any attribution limits. Do not restart the deleted VMs to obtain Billing evidence.

Source: [Google Cloud: Analyze billing data and cost trends with Reports](https://docs.cloud.google.com/billing/docs/how-to/reports), especially CSV download, cost columns, data availability and reporting delays.
