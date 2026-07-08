// Feature (KNOWN GAP): a file-scoped namespace (`namespace X;`) — the default in .NET 6+.
// Correct semantics: the namespace qualifies every type in the file, so the FQN is
// `Ledger.Account` (ADR-003 D3). The adapter only propagates the namespace for BLOCK-form
// declarations; under a file-scoped namespace the type declarations are SIBLINGS of the
// file_scoped_namespace_declaration node in the tree-sitter grammar, so they are walked
// with no namespace and come out unqualified (`Account`). Because the FQN is the symbol's
// identity, every symbol/edge key mismatches -> this fixture scores far below 1.0 until
// the adapter handles file-scoped namespaces. Undocumented adapter gap surfaced by the
// C# conformance batch (see docs/conformance-fixture-conventions.md#known-gaps).
namespace Ledger;

public class Account
{
    public Account(decimal opening)
    {
        Balance = opening;
    }

    public decimal Balance { get; set; }

    public decimal Deposit(decimal amount)
    {
        return Add(amount);
    }

    private decimal Add(decimal amount)
    {
        return Balance + amount;
    }
}
