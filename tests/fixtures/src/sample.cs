using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace MyApp.Services
{
    public enum ProcessStatus
    {
        Pending,
        Running,
        Done,
        Failed,
    }

    public interface IDataService
    {
        string Fetch(int id);
        Task<string> FetchAsync(int id);
        int Count { get; }
    }

    public class DataServiceBase
    {
        protected string BaseTag { get; set; } = "base";

        protected virtual string Format(string raw)
        {
            return raw.Trim();
        }
    }

    // partial class — second declaration lives in DataService.Ext.cs (separate file)
    public partial class DataService : DataServiceBase, IDataService
    {
        private readonly List<string> _cache;

        public DataService(List<string> cache)
        {
            _cache = cache;
        }

        public DataService(List<string> cache, int capacity)
        {
            _cache = cache;
            _cache.Capacity = capacity;
        }

        public int Count { get; set; }

        public ProcessStatus Status { get; set; } = ProcessStatus.Pending;

        [Authorize]
        public string Fetch(int id)
        {
            return Format(_cache[id]);
        }

        [Authorize]
        [AllowAnonymous]
        public string Fetch(int id, bool force)
        {
            if (force) _cache.Clear();
            return Fetch(id);
        }

        public async Task<string> FetchAsync(int id)
        {
            await Task.Delay(0);
            return Fetch(id);
        }

        public string Describe()
        {
            return $"{Count} items, status={Status}";
        }

        public class Config
        {
            public int MaxRetries { get; set; } = 3;
        }
    }
}
