// Feature (KNOWN GAP): a class implementing two interfaces with NO base class.
// Correct semantics: BOTH IReader and IWriter are `implements` edges. The C# adapter
// lacks type resolution, so it applies the base-list heuristic "first entry -> extends,
// rest -> implements" and mislabels IReader as `extends`. This fixture's expected.json
// encodes the CORRECT semantics (2x implements); it is therefore expected to score below
// 1.0 on all-edges — that sub-1.0 is the ruler honestly catching a documented adapter
// limitation (see src/adapters/csharp_adapter.py "Documented limits: base-list").
namespace Net
{
    public interface IReader
    {
        int Read();
    }

    public interface IWriter
    {
        void Write(int b);
    }

    public class Foo : IReader, IWriter
    {
        public int Read()
        {
            return 0;
        }

        public void Write(int b)
        {
        }
    }
}
