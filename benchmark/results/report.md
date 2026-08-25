# datafile.py benchmark

## Store sizes

| size | bytes | records |
|---|---|---|
| 1MB | 1,048,572 | 5,491 |
| 10MB | 10,485,739 | 54,886 |
| 100MB | 104,857,451 | 548,831 |
| 500MB | 524,287,916 | 2,744,157 |
| 1GB | 1,073,741,717 | 5,620,027 |
| 2GB | 2,147,483,468 | 11,240,054 |

## Wall time

| action | 1MB | 10MB | 100MB | 500MB | 1GB | 2GB |
|---|---|---|---|---|---|---|
| `schema` | 173ms | 111ms | 117ms | 175ms | 182ms | 172ms |
| `get (cold idx)` | 176ms | 224ms | 1.4s | 7.2s | 13.6s | 28.3s |
| `get (warm idx)` | 117ms | 117ms | 288ms | 1.6s | 3.4s | 7.5s |
| `keys` | 117ms | 283ms | 1.7s | 8.6s | 17.1s | 36.5s |
| `list --limit 100` | 176ms | 403ms | 3.7s | 18.9s | 50.1s | 125.7s |
| `validate` | 176ms | 399ms | 3.7s | 18.8s | 47.7s | 119.8s |
| `put (1 record)` | 114ms | 117ms | 287ms | 1.5s | 3.7s | 7.8s |
| `delete` | 117ms | 176ms | 399ms | 1.8s | 4.5s | 9.2s |
| `compact` | 173ms | 629ms | 5.9s | 32.9s | 81.9s | 238.1s |
| `repair` | 224ms | 931ms | 10.2s | 68.5s | 165.2s | **TIMEOUT** |

## Peak RSS

| action | 1MB | 10MB | 100MB | 500MB | 1GB | 2GB |
|---|---|---|---|---|---|---|
| `schema` | 36MB | 36MB | 36MB | 36MB | 36MB | 36MB |
| `get (cold idx)` | 38MB | 51MB | 173MB | 702MB | 1.6GB | 3.3GB |
| `get (warm idx)` | 38MB | 53MB | 190MB | 829MB | 1.8GB | 3.7GB |
| `keys` | 38MB | 56MB | 190MB | 829MB | 1.8GB | 3.7GB |
| `list --limit 100` | 46MB | 137MB | 1.0GB | 4.9GB | 6.1GB | 7.6GB |
| `validate` | 46MB | 137MB | 1.0GB | 4.6GB | 6.1GB | 8.7GB |
| `put (1 record)` | 38MB | 58MB | 229MB | 989MB | 2.5GB | 4.9GB |
| `delete` | 38MB | 53MB | 211MB | 870MB | 1.9GB | 3.7GB |
| `compact` | 49MB | 171MB | 1.3GB | 5.5GB | 6.3GB | 8.0GB |
| `repair` | 59MB | 270MB | 2.3GB | 5.3GB | 7.4GB | **TIMEOUT** |

## stdout size

| action | 1MB | 10MB | 100MB | 500MB | 1GB | 2GB |
|---|---|---|---|---|---|---|
| `schema` | 344B | 344B | 344B | 344B | 344B | 344B |
| `get (cold idx)` | 188B | 212B | 191B | 207B | 208B | 206B |
| `get (warm idx)` | 188B | 212B | 191B | 207B | 208B | 206B |
| `keys` | 198.4KB | 1.9MB | 19.4MB | 96.8MB | 198.3MB | 396.6MB |
| `list --limit 100` | 7.5KB | 7.5KB | 7.5KB | 7.5KB | 7.5KB | 7.5KB |
| `validate` | 19B | 20B | 21B | 22B | 22B | 23B |
| `put (1 record)` | 95B | 95B | 95B | 95B | 95B | 95B |
| `delete` | 192B | 193B | 194B | 194B | 192B | 192B |
| `compact` | 34B | 36B | 38B | 40B | 40B | 41B |
| `repair` | 26B | 27B | 28B | 29B | 29B | **TIMEOUT** |
