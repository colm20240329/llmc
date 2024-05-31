## Test speed for some kernels

llama2-7B-chat, input_len=20，output_len=400，4bit(Q4_K_M)，3bit(Q3_K_M)，2bit(Q2_K_M)


### llama.cpp gpu kernel

| Bit         | prefill (ms/token) | decode (ms/token) |
|-------------|--------------------|-------------------|
| 4           | 2.27               | 7.13              |
| 3           | 3.57               | 8.73              |
| 2           | 2.96               | 7.93              |

### llama.cpp cpu kernel

| Bit         | prefill (ms/token) | decode (ms/token) |
|-------------|--------------------|-------------------|
| 4           | 12.55              | 52.66             |
| 3           | 13.03              | 49.28             |
| 2           | 11.28              | 45.69             |