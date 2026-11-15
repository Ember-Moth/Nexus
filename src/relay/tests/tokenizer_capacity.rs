//! Opt-in P5 measurement of the local text estimator: cold and warm encoder, long text and
//! concurrent counting. The estimator is not on the request path today (generation reserves
//! by deployment bounds; `count_tokens` asks the upstream), so this bounds the cost of using it.
//!
//! ```sh
//! cargo test --release --locked --test tokenizer_capacity -- --ignored --nocapture
//! ```
//!
//! Each run is a fresh process, so the first call per encoding pays the cold load of its BPE
//! ranks. Prints one `tokenizer_capacity {json}` line per encoding for scripts/relay_load.py.
use nexus_relay::protocol::tokens::Encoding;
use std::time::{Duration, Instant};

const SAMPLE: &str = "The relay reserves funds before it forwards a request. \
    节点在转发请求之前预留资金。 0123456789 {\"tool\":\"lookup\",\"q\":\"日本\"}\n";

fn text(bytes: usize) -> String {
    let mut out = String::with_capacity(bytes + SAMPLE.len());
    while out.len() + SAMPLE.len() <= bytes {
        out.push_str(SAMPLE);
    }
    while out.len() < bytes {
        out.push('x');
    }
    out
}

fn rss_mib() -> f64 {
    let out = std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", &std::process::id().to_string()])
        .output()
        .expect("ps");
    String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<f64>()
        .unwrap_or(0.0)
        / 1024.0
}

fn quantiles(mut samples: Vec<Duration>) -> String {
    samples.sort();
    let at = |q: f64| {
        let i = ((samples.len() as f64 * q).ceil() as usize).clamp(1, samples.len()) - 1;
        samples[i].as_secs_f64() * 1000.0
    };
    format!(
        "{{\"n\":{},\"p50\":{:.3},\"p99\":{:.3},\"max\":{:.3}}}",
        samples.len(),
        at(0.5),
        at(0.99),
        at(1.0)
    )
}

#[test]
#[ignore = "capacity measurement; run with --ignored --nocapture"]
fn tokenizer_capacity() {
    let threads = std::thread::available_parallelism().map_or(4, |n| n.get());
    for (name, encoding) in [
        ("o200k_base", Encoding::O200kBase),
        ("cl100k_base", Encoding::Cl100kBase),
    ] {
        let before = rss_mib();
        let started = Instant::now();
        let warm = encoding.estimate("hello", 64).expect("estimate").0;
        let cold_ms = started.elapsed().as_secs_f64() * 1000.0;
        let loaded = rss_mib();
        assert_eq!(warm, 1);
        let mut sizes = Vec::new();
        for (bytes, runs) in [
            (1 << 10, 2000),
            (64 << 10, 200),
            (1 << 20, 20),
            (2 << 20, 10),
        ] {
            let input = text(bytes);
            let mut samples = Vec::with_capacity(runs);
            let mut tokens = 0;
            for _ in 0..runs {
                let t = Instant::now();
                tokens = encoding.estimate(&input, bytes).expect("estimate").0;
                samples.push(t.elapsed());
            }
            sizes.push(format!(
                "{{\"bytes\":{bytes},\"tokens\":{tokens},\"ms\":{}}}",
                quantiles(samples)
            ));
        }
        // Every core counts 64 KiB texts at once, as a burst of count requests would.
        let input = text(64 << 10);
        let started = Instant::now();
        let samples: Vec<Duration> = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..threads)
                .map(|_| {
                    scope.spawn(|| {
                        (0..100)
                            .map(|_| {
                                let t = Instant::now();
                                encoding.estimate(&input, input.len()).expect("estimate");
                                t.elapsed()
                            })
                            .collect::<Vec<_>>()
                    })
                })
                .collect();
            workers
                .into_iter()
                .flat_map(|w| w.join().expect("worker"))
                .collect()
        });
        let elapsed = started.elapsed().as_secs_f64();
        let count = samples.len();
        println!(
            "tokenizer_capacity {{\"encoding\":\"{name}\",\"cold_first_call_ms\":{cold_ms:.1},\
             \"rss_before_mib\":{before:.1},\"rss_after_load_mib\":{loaded:.1},\
             \"rss_end_mib\":{:.1},\"sizes\":[{}],\"concurrent\":{{\"threads\":{threads},\
             \"bytes\":65536,\"per_second\":{:.0},\"ms\":{}}}}}",
            rss_mib(),
            sizes.join(","),
            count as f64 / elapsed,
            quantiles(samples)
        );
    }
}
