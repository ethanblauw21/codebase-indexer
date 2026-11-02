// Feature: Apps Script — top-level functions in one shared global scope. No imports:
// recalculate() calls getEstimateSheet()/sumColumn() from helpers.gs by name, and
// helpers.gs calls currencySymbol() back. Google services (SpreadsheetApp) are globals
// the project does not define.

function onOpen() {
  SpreadsheetApp.getUi().createMenu("Estimates").addItem("Recalculate", "recalculate").addToUi();
}

function recalculate() {
  const sheet = getEstimateSheet();
  const total = sumColumn(sheet, 3);
  sheet.getRange("F1").setValue(formatCurrency(total));
}

function currencySymbol() {
  return "$";
}
