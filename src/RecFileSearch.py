import os

def find_largest_file(start_path: str) -> tuple[str | None, int]:
    """
    Recursively scans a directory using os.scandir to find the largest file.
    Designed for high-throughput I/O with strict error and symlink handling.
    """
    largest_file = None
    max_size = -1

    def _scan_directory(directory: str):
        nonlocal largest_file, max_size
        
        try:
            # os.scandir is an iterator, keeping RAM usage near zero
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    try:
                        # Explicitly ignore symlinks to prevent loops
                        if entry.is_file(follow_symlinks=False):
                            # Entry.stat() is cached on most OSes during scandir
                            size = entry.stat(follow_symlinks=False).st_size
                            if size > max_size:
                                max_size = size
                                largest_file = entry.path
                                
                        elif entry.is_dir(follow_symlinks=False):
                            _scan_directory(entry.path)
                            
                    except (OSError, PermissionError):
                        # Swallow errors for inaccessible individual files
                        continue
                        
        except (OSError, PermissionError):
            # Swallow errors for restricted directories (e.g., System Volume Information)
            pass

    print(f"Initiating high-speed scan on: {start_path}")
    _scan_directory(start_path)
    
    return largest_file, max_size

if __name__ == "__main__":
    # Change to '/' for Linux/Mac or 'C:\\' for Windows
    target_drive = "V:\\" if os.name == "nt" else "/"
    
    file_path, size_bytes = find_largest_file(target_drive)
    
    if file_path:
        size_gb = size_bytes / (1024 ** 3)
        print(f"\nLargest File Found:")
        print(f"Path: {file_path}")
        print(f"Size: {size_gb:.2f} GB ({size_bytes} bytes)")
    else:
        print("\nNo files found or accessible in the specified path.")