// Feature: Apps Script — the other half of menu.gs. Calls back into menu.gs by name.

function getEstimateSheet() {
  return SpreadsheetApp.getActive().getSheetByName("Estimates");
}

function sumColumn(sheet, column) {
  const values = sheet.getRange(2, column, sheet.getLastRow() - 1, 1).getValues();
  let total = 0;
  for (const row of values) {
    total += row[0];
  }
  return total;
}

function formatCurrency(amount) {
  return currencySymbol() + amount.toFixed(2);
}
