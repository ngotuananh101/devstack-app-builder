# Redis runtime builder

Build Redis core from SHA-256-authenticated upstream source with a native Linux x86_64 compiler. No Docker, profiles or DevStack service configuration.

```bash
./build.sh redis                 # musl, Redis 8.10.3
./build.sh redis musl 8.10.3      # fully static
./build.sh redis glibc 8.10.3     # native glibc
./build.sh redis --print-plan    # inspect inputs without building
```

See [Redis build and artifact contracts](../../docs/runtimes/redis.md).
