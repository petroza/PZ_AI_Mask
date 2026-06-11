PZ_MASK v67 — FAST ENCODE + HW STATUS + NO AUTO HQ FORCE

Změny:
- RUN MASK už automaticky nezaškrtává MatAnyone ani Refine Edge.
- API už nevynucuje sam_model=hiera_large, matte_enabled=1 ani refine_mode=hq.
- Worker config: auto_hq=false, auto_hq_force_large=false.
- Nové joby startují rychleji: Hiera Base+, MatAnyone OFF, Refine OFF, refine_mode=fast.
- Auto Edge Fix zůstává jako ruční volba, ale počítá se jen když ho zapneš.
- HW status: worker zapisuje i lokální storage/worker_status.json, takže horní lišta má data i když heartbeat přes API selže.
- Rychlé enkódování zůstává: H.264 veryfast + CRF 12.
