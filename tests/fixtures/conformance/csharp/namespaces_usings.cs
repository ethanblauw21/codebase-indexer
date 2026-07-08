// Feature: multiple using directives -> one import edge per using, under a block-form
// namespace (correctly qualifies symbols). A static utility class whose public method
// calls a private helper (call edge); `new StringBuilder()` is object creation, NOT an
// invocation, so it is correctly absent from the call edges.
// (File-scoped namespaces are covered separately by filescoped_namespace.cs, which
// documents a real qualification gap.)
using System;
using System.Text;
using System.Collections.Generic;

namespace App.Util
{
    public static class Slugify
    {
        public static string Run(string input)
        {
            var sb = new StringBuilder();
            return Normalize(sb, input);
        }

        private static string Normalize(StringBuilder sb, string input)
        {
            return input;
        }
    }
}
