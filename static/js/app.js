fetch("/api/loans").then(r=>r.json()).then(x=>document.querySelector("#loans").textContent=`Loans in database: ${x.length}`);
