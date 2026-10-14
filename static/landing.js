(function(){
"use strict";
var $=function(s,r){return (r||document).querySelector(s)},$$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var reduce=window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* headline: word-by-word entrance */
var h1=$(".hero h1");
if(h1&&!reduce){
  var words=h1.textContent.trim().split(/\s+/);
  h1.innerHTML=words.map(function(w,i){return '<span class="w" style="--i:'+i+'">'+w.replace(/</g,"&lt;")+'</span>'}).join(" ");
}

/* nav: stuck state + scroll progress + parallax blobs */
var nav=$("#nav"),bar=$("#progress"),b1=$(".b1"),b2=$(".b2"),ticking=false;
function onScroll(){
  var y=window.scrollY,h=document.documentElement.scrollHeight-window.innerHeight;
  nav.classList.toggle("stuck",y>30);
  bar.style.transform="scaleX("+(h>0?y/h:0)+")";
  if(!reduce&&y<1100){b1.style.transform="translateY("+(y*.18)+"px)";b2.style.transform="translateY("+(-y*.12)+"px)"}
  ticking=false;
}
window.addEventListener("scroll",function(){if(!ticking){ticking=true;requestAnimationFrame(onScroll)}},{passive:true});
onScroll();
$$("#menu a").forEach(function(a){a.addEventListener("click",function(){document.body.classList.remove("menu-open")})});

/* hero carousel */
var stage=$("#carousel");
if(stage){
  var shots=$$(".shot",stage),pshots=$$(".pshot",stage),dots=$$(".dots button",stage),cap=$(".caption",stage),
      capT=$("#capT"),capD=$("#capD"),cur=0,DUR=5200,timer=null,hover=false;
  stage.style.setProperty("--dur",DUR+"ms");
  function show(i){
    cur=(i+shots.length)%shots.length;
    shots.forEach(function(s,k){s.classList.toggle("on",k===cur)});
    pshots.forEach(function(s,k){s.classList.toggle("on",k===cur%pshots.length)});
    dots.forEach(function(d,k){d.classList.remove("on");d.classList.toggle("done",k<cur);});
    void stage.offsetWidth;
    dots[cur].classList.add("on");
    cap.classList.add("swap");
    setTimeout(function(){capT.textContent=LP.slides[cur][0];capD.textContent=LP.slides[cur][1];cap.classList.remove("swap")},220);
    clearTimeout(timer);
    if(!reduce&&!hover)timer=setTimeout(function(){show(cur+1)},DUR);
  }
  dots.forEach(function(d,k){d.addEventListener("click",function(){show(k)})});
  $(".arrow.next",stage).addEventListener("click",function(){show(cur+1)});
  $(".arrow.prev",stage).addEventListener("click",function(){show(cur-1)});
  stage.addEventListener("mouseenter",function(){hover=true;stage.classList.add("paused");clearTimeout(timer)});
  stage.addEventListener("mouseleave",function(){hover=false;stage.classList.remove("paused");show(cur)});
  var dir=document.documentElement.dir==="rtl"?-1:1,sx=null;
  stage.addEventListener("touchstart",function(e){sx=e.touches[0].clientX},{passive:true});
  stage.addEventListener("touchend",function(e){if(sx===null)return;var dx=e.changedTouches[0].clientX-sx;if(Math.abs(dx)>40)show(cur+(dx<0?1:-1)*dir);sx=null});
  document.addEventListener("visibilitychange",function(){if(document.hidden)clearTimeout(timer);else show(cur)});
  show(0);
  /* pointer tilt */
  var br=$(".browser",stage);
  if(!reduce&&window.matchMedia("(hover:hover)").matches){
    stage.addEventListener("mousemove",function(e){
      var r=stage.getBoundingClientRect(),x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
      br.style.transform="rotateY("+(x*9)+"deg) rotateX("+(-y*7)+"deg)";
    });
    stage.addEventListener("mouseleave",function(){br.style.transform=""});
  }
}

/* reveal on scroll */
var rv=$$(".rv");
if("IntersectionObserver" in window&&!reduce){
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target)}})},{threshold:.12,rootMargin:"0px 0px -6% 0px"});
  rv.forEach(function(el){io.observe(el)});
}else rv.forEach(function(el){el.classList.add("in")});

/* count-up */
var cnt=$$(".cnt");
if("IntersectionObserver" in window&&!reduce){
  cnt.forEach(function(c){c.textContent="0"});
  var co=new IntersectionObserver(function(es){es.forEach(function(e){
    if(!e.isIntersecting)return;co.unobserve(e.target);
    var to=+e.target.dataset.to,t0=null;
    function step(t){if(!t0)t0=t;var p=Math.min(1,(t-t0)/1400),v=Math.round(to*(1-Math.pow(1-p,3)));e.target.textContent=v;if(p<1)requestAnimationFrame(step)}
    requestAnimationFrame(step);
  })},{threshold:.6});
  cnt.forEach(function(c){co.observe(c)});
}

/* module tabs */
var tabs=$("#tabs");
if(tabs){
  var tb=$$(".tabbar button",tabs),ti=$$(".timg",tabs),tt=$("#tabT"),td=$("#tabD"),tx=$(".tabtext",tabs),auto=null;
  function pick(i){
    tb.forEach(function(b,k){b.classList.toggle("on",k===i);b.setAttribute("aria-selected",k===i)});
    ti.forEach(function(m,k){m.classList.toggle("on",k===i)});
    tx.classList.add("swap");
    setTimeout(function(){tt.textContent=LP.tabs[i][0];td.textContent=LP.tabs[i][1];tx.classList.remove("swap")},180);
  }
  tb.forEach(function(b,k){b.addEventListener("click",function(){clearInterval(auto);pick(k)})});
}

/* FAQ: one open at a time */
var det=$$(".faq details");
det.forEach(function(d){d.addEventListener("toggle",function(){if(d.open)det.forEach(function(o){if(o!==d)o.open=false})})});
})();
