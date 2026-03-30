import re

# Read original file
with open('/home/claude/socialpulse/frontend/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the generatePDF function - from "async function generatePDF(){" to the next "// ===== INIT ====="
start_marker = "async function generatePDF(){"
end_marker = "// ===== INIT ====="

start_idx = content.index(start_marker)
end_idx = content.index(end_marker)

new_pdf_func = r'''async function generatePDF(){
  if(posts.length===0){toast('error','Error','No data for report');return;}
  
  // Show loading
  toast('info','Generating PDF...','Menggunakan AI untuk narasi profesional...');
  
  const keyword = currentProject?.name || 'Report';
  const formatNum = n => {if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return(n||0).toString();};
  const formatPct = (n,t) => Math.round((n/(t||1))*100);
  
  // ===== CALCULATE ALL STATS =====
  const stats = {total:posts.length,views:0,likes:0,shares:0,comments:0,positive:0,negative:0,neutral:0};
  const platforms = {};const keywords = {};const sources = {};const cities = {};const authorMap = {};
  const dateMap = {};const platSentiment = {};const dailyMentions = {};
  
  posts.forEach(p => {
    stats.views += p.views||0;stats.likes += p.likes||0;stats.shares += p.shares||0;stats.comments += p.comments||0;
    stats[p.sentiment||'neutral']++;
    platforms[p.platform] = (platforms[p.platform]||0)+1;
    if(p.source_name) sources[p.source_name] = (sources[p.source_name]||0)+1;
    const tags = (p.content||'').match(/#[\w\d_]+/gi)||[];
    tags.forEach(t => keywords[t.toLowerCase()] = (keywords[t.toLowerCase()]||0)+1);
    const postCities = p.cities ? (Array.isArray(p.cities)?p.cities:JSON.parse(p.cities||'[]')) : findCities(p.content);
    postCities.forEach(c => cities[c] = (cities[c]||0)+1);
    if(!authorMap[p.author])authorMap[p.author]={views:0,posts:0,positive:0,negative:0,neutral:0,platform:p.platform,likes:0,shares:0,comments:0};
    authorMap[p.author].views+=p.views||0;authorMap[p.author].posts++;authorMap[p.author][p.sentiment||'neutral']++;
    authorMap[p.author].likes+=p.likes||0;authorMap[p.author].shares+=p.shares||0;authorMap[p.author].comments+=p.comments||0;
    // Platform sentiment
    if(!platSentiment[p.platform])platSentiment[p.platform]={positive:0,negative:0,neutral:0};
    platSentiment[p.platform][p.sentiment||'neutral']++;
    // Daily mentions
    const day = p.timestamp ? new Date(p.timestamp).toISOString().split('T')[0] : 'unknown';
    if(!dailyMentions[day])dailyMentions[day]={};
    dailyMentions[day][p.platform] = (dailyMentions[day][p.platform]||0)+1;
  });
  
  const total = stats.total||1;
  const engagement = stats.likes+stats.comments+stats.shares;
  const avgEngPerPost = Math.round(engagement/total);
  const platEntries = Object.entries(platforms).filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1]);
  const maxPlat = Math.max(...platEntries.map(([,v])=>v),1);
  const topInfluencers = Object.entries(authorMap).sort((a,b)=>b[1].views-a[1].views).slice(0,10);
  const sortedKeywords = Object.entries(keywords).sort((a,b)=>b[1]-a[1]).slice(0,20);
  const sortedCities = Object.entries(cities).sort((a,b)=>b[1]-a[1]).slice(0,10);
  const sortedSources = Object.entries(sources).sort((a,b)=>b[1]-a[1]).slice(0,10);
  const totalReach = topInfluencers.reduce((s,[,d])=>s+d.views,0)||1;
  const posPct = formatPct(stats.positive,total);
  const negPct = formatPct(stats.negative,total);
  const neuPct = formatPct(stats.neutral,total);
  const topPlat = platEntries[0]?.[0]||'unknown';
  const sentScore = stats.positive - stats.negative;
  
  // ===== CALL OPENROUTER AI FOR NARRATIVE =====
  let aiData = null;
  try {
    const orToken = await getORToken();
    if(orToken){
      toast('info','AI Writing...','Generating narrative content with AI...');
      const dataSummary = `Keyword: ${keyword}
Periode: ${dateFrom} sampai ${dateTo}
Total Mentions: ${stats.total}
Total Views/Reach: ${stats.views}
Total Likes: ${stats.likes} | Shares: ${stats.shares} | Comments: ${stats.comments}
Avg Engagement per post: ${avgEngPerPost}
Sentimen: Positif ${stats.positive} (${posPct}%), Netral ${stats.neutral} (${neuPct}%), Negatif ${stats.negative} (${negPct}%)
Sentiment Score: ${sentScore>=0?'+':''}${sentScore}
Platform: ${platEntries.map(([p,c])=>p+' '+c+' ('+formatPct(c,total)+'%)').join(', ')}
Platform Sentiment: ${Object.entries(platSentiment).map(([p,s])=>p+': positif '+s.positive+' netral '+s.neutral+' negatif '+s.negative).join(', ')}
Top Hashtags: ${sortedKeywords.slice(0,15).map(([h,c])=>h+' ('+c+')').join(', ')}
Top Influencers: ${topInfluencers.map(([n,d])=>n+' ('+d.views+' views, '+d.posts+' posts, '+d.platform+', SOV: '+Math.round(d.views/totalReach*100)+'%)').join(', ')}
Top Cities: ${sortedCities.map(([c,n])=>c+' ('+n+')').join(', ')}
Top Content: ${[...posts].sort((a,b)=>(b.views||0)-(a.views||0)).slice(0,8).map(p=>p.author+' ['+p.platform+'] '+(p.content||'').substring(0,80)+' ('+p.views+' views, '+p.sentiment+')').join(' | ')}`;

      const sysPrompt = `Kamu adalah analis media sosial profesional senior. Buat analisis mendalam dalam Bahasa Indonesia untuk laporan PDF klien. Format output HARUS JSON valid tanpa backticks:
{"executive_summary":"3-5 paragraf analisis mendalam tentang performa brand termasuk metrik utama, tren, dan insight. Gunakan data spesifik dan angka dari data yang diberikan. Tulis seperti laporan konsultan profesional.","tren_mentions":"2-3 paragraf tentang tren mentions harian, kapan puncak terjadi, apa pemicunya, dan pola yang terlihat.","analisis_engagement":"2 paragraf tentang engagement rate, perbandingan likes vs comments vs shares, dan apa artinya untuk brand.","analisis_sentiment":"2-3 paragraf mendalam tentang distribusi sentimen, per platform, driver sentimen positif, dan potensi risiko negatif.","analisis_platform":"2 paragraf tentang distribusi platform, mana yang dominan, mana yang perlu dikembangkan.","analisis_influencer":"2 paragraf tentang top influencers, ketergantungan pada influencer tertentu, dan rekomendasi.","analisis_hashtag":"1-2 paragraf tentang trending hashtags dan bagaimana memanfaatkannya.","analisis_lokasi":"1 paragraf tentang distribusi geografis mentions.","swot":{"strengths":["3-4 poin detail dengan data spesifik"],"weaknesses":["3-4 poin detail"],"opportunities":["3-4 poin detail"],"threats":["3-4 poin detail"]},"recommendations":["6 rekomendasi strategis detail dengan penjelasan actionable masing-masing 2-3 kalimat"],"key_findings":["5-6 temuan utama dengan angka spesifik"]}`;

      const resp = await callOpenRouter(sysPrompt, 'Analisis data social media monitoring berikut:\n\n'+dataSummary);
      if(resp){
        try{
          const cleaned = resp.replace(/```json\s*/g,'').replace(/```\s*/g,'').trim();
          aiData = JSON.parse(cleaned);
        }catch(e){
          const jsonMatch = resp.match(/\{[\s\S]*\}/);
          if(jsonMatch) aiData = JSON.parse(jsonMatch[0]);
        }
      }
    }
  }catch(e){ console.warn('AI analysis failed, using auto-generated text:', e); }
  
  // ===== AUTO-GENERATE FALLBACK TEXT =====
  if(!aiData) {
    aiData = {
      executive_summary: `${keyword} mencatatkan ${stats.total} mentions dengan social media reach ${formatNum(stats.views)} selama periode ${dateFrom} sampai ${dateTo}. Sentimen ${posPct>=50?'positif mendominasi':'cenderung netral'} dengan ${stats.positive} mentions positif (${posPct}%)${negPct===0?' dan nol mentions negatif — pencapaian yang sangat baik':' serta '+stats.negative+' mentions negatif ('+negPct+'%)'}. ${platEntries[0]?.[0]?.charAt(0).toUpperCase()+platEntries[0]?.[0]?.slice(1)||'Platform utama'} menjadi platform dominan dengan ${platEntries[0]?.[1]||0} mentions (${formatPct(platEntries[0]?.[1]||0,total)}% total). Average engagement per post mencapai ${avgEngPerPost} interactions menunjukkan ${avgEngPerPost>100?'kualitas engagement yang baik':'ruang untuk peningkatan engagement'}.\n\nTop influencer ${topInfluencers[0]?.[0]||'N/A'} menyumbang ${formatNum(topInfluencers[0]?.[1]?.views||0)} views dengan Share of Voice ${Math.round((topInfluencers[0]?.[1]?.views||0)/totalReach*100)}%. Trending hashtag utama ${sortedKeywords[0]?.[0]||'N/A'} muncul ${sortedKeywords[0]?.[1]||0} kali${sortedCities.length>0?' dan aktivitas tertinggi terdeteksi di '+sortedCities[0][0]+' dengan '+sortedCities[0][1]+' mentions':''}.`,
      tren_mentions: `Volume mentions ${keyword} menunjukkan ${Object.keys(dailyMentions).length>0?'aktivitas yang bervariasi sepanjang periode monitoring':'data yang perlu dikumpulkan lebih lama'}. Total ${stats.total} mentions tersebar di ${Object.keys(platforms).filter(p=>platforms[p]>0).length} platform berbeda.`,
      analisis_engagement: `Total engagement mencapai ${formatNum(engagement)} interactions dari ${stats.total} mentions. Breakdown: ${formatNum(stats.likes)} likes, ${formatNum(stats.comments)} comments, dan ${formatNum(stats.shares)} shares. Average engagement per post: ${avgEngPerPost} interactions. Likes per post: ${Math.round(stats.likes/total)} | Comments per post: ${Math.round(stats.comments/total)} | Shares per post: ${Math.round(stats.shares/total)}.`,
      analisis_sentiment: `Distribusi sentimen menunjukkan ${posPct}% positif, ${neuPct}% netral, dan ${negPct}% negatif${negPct===0?'. Zero negative mentions merupakan pencapaian luar biasa':''}.`,
      analisis_platform: `${platEntries[0]?.[0]||'Platform'} mendominasi dengan ${formatPct(platEntries[0]?.[1]||0,total)}% total mentions. ${platEntries.length>1?platEntries[1][0]+' menjadi platform kedua dengan '+platEntries[1][1]+' mentions.':''}`,
      analisis_influencer: `${topInfluencers[0]?.[0]||'Top influencer'} memimpin dengan ${formatNum(topInfluencers[0]?.[1]?.views||0)} views dan SOV ${Math.round((topInfluencers[0]?.[1]?.views||0)/totalReach*100)}%.`,
      swot: {
        strengths: [`Sentimen positif ${posPct}%${negPct===0?' dengan zero negatif':''}`,`${formatNum(stats.views)} total reach dari ${stats.total} mentions`,`Top influencer: ${topInfluencers[0]?.[0]||'N/A'} (${formatNum(topInfluencers[0]?.[1]?.views||0)} reach)`],
        weaknesses: [`Platform ${platEntries.length>1?platEntries[platEntries.length-1]?.[0]:'news'} masih rendah`,`Engagement rate bisa ditingkatkan`,`Ketergantungan pada top influencer`],
        opportunities: [`Ekspansi ke platform yang belum aktif`,`Manfaatkan momentum trending hashtag: ${sortedKeywords[0]?.[0]||'N/A'}`,`Perkuat kehadiran di ${sortedCities[0]?.[0]||'kota utama'}`],
        threats: [`Kompetitor bisa meningkatkan presence`,`Perubahan algoritma platform ${topPlat}`,`Potensi sentimen negatif jika isu tidak ditangani`]
      },
      recommendations: [
        `Sentimen ${posPct>=50?'positif mendominasi — pertahankan strategi':'cenderung netral — buat konten lebih engaging'}.`,
        `Optimalkan konten di ${topPlat} (${formatPct(platEntries[0]?.[1]||0,total)}% mentions) dengan format yang lebih engaging.`,
        `Perluas influencer pool — kurangi ketergantungan pada top 3 influencer yang menguasai SOV.`,
        `Manfaatkan trending hashtag ${sortedKeywords[0]?.[0]||'N/A'} (${sortedKeywords[0]?.[1]||0} mentions) untuk kampanye.`,
        `Tingkatkan presence di ${sortedCities[0]?.[0]||'kota utama'} yang menunjukkan aktivitas tinggi.`,
        `Diversifikasi platform untuk mengurangi risiko perubahan algoritma.`
      ],
      key_findings: [
        `Total ${stats.total} mentions ditemukan di ${Object.keys(platforms).filter(p=>platforms[p]>0).length} platform.`,
        `Platform terbesar: ${topPlat.charAt(0).toUpperCase()+topPlat.slice(1)} (${platEntries[0]?.[1]||0} posts, ${formatPct(platEntries[0]?.[1]||0,total)}%).`,
        `Sentimen: ${posPct}% positif, ${neuPct}% netral, ${negPct}% negatif.`,
        `Top reach: ${topInfluencers[0]?.[0]||'N/A'} dengan ${formatNum(topInfluencers[0]?.[1]?.views||0)} views.`
      ]
    };
  }
  
  // ===== JSPDF SETUP =====
  const{jsPDF}=window.jspdf;const doc=new jsPDF('p','mm','a4');
  const pw=210,ph=297,m=15;
  const pink=[236,72,153],dark=[18,18,26],darkCard=[26,26,36],gray=[156,163,175],lightGray=[200,200,200];
  const green=[16,185,129],red=[239,68,68],yellow=[245,158,11],accent=[99,102,241],white=[255,255,255];
  const tiktok=[0,242,234],twitter=[29,161,242],instagram=[225,48,108];
  const facebook=[24,119,242],youtube=[255,0,0],news=[249,115,22];
  const platformColors={tiktok,twitter,instagram,facebook,youtube,news};
  const treemapColors=[pink,accent,green,tiktok,twitter,yellow,red,[139,92,246],[14,184,166],[234,179,8],[249,115,22],[132,204,22]];
  
  const setC=c=>doc.setTextColor(c[0],c[1],c[2]);
  const setF=c=>doc.setFillColor(c[0],c[1],c[2]);
  const setD=c=>doc.setDrawColor(c[0],c[1],c[2]);
  let pageNum=0;
  const TP=16;
  
  // Helper: draw page background & header
  const drawPage=(title,pn)=>{
    if(pn>1)doc.addPage();
    pageNum=pn;
    setF(dark);doc.rect(0,0,pw,ph,'F');
    // Gradient top bar
    setF(pink);doc.rect(0,0,pw*0.5,4,'F');setF(accent);doc.rect(pw*0.5,0,pw*0.5,4,'F');
    // Title
    doc.setFontSize(16);doc.setFont('helvetica','bold');setC(white);doc.text(title,m,20);
    // Page info
    doc.setFontSize(8);setC(gray);doc.text(`Page ${pn}/${TP}`,pw-m,20,{align:'right'});
    // Footer
    doc.setFontSize(7);setC(gray);
    doc.text(`SocialPulse Pro | ${keyword} | ${dateFrom} - ${dateTo}`,pw/2,ph-8,{align:'center'});
  };
  
  // Helper: draw card box
  const drawBox=(x,y,w,h,opts={})=>{
    setF(darkCard);doc.roundedRect(x,y,w,h,2,2,'F');
    if(opts.topColor){setF(opts.topColor);doc.rect(x,y,w,2.5,'F');}
    if(opts.title){doc.setFontSize(10);doc.setFont('helvetica','bold');setC(white);doc.text(opts.title,x+6,y+12);}
  };
  
  // Helper: wrap text and return lines
  const wrapText=(text,maxWidth,fontSize=9)=>{
    doc.setFontSize(fontSize);doc.setFont('helvetica','normal');
    return doc.splitTextToSize(text||'',maxWidth);
  };
  
  // Helper: draw paragraph text block, returns new Y
  const drawParagraph=(text,x,y,maxWidth,fontSize=9,lineHeight=4.5)=>{
    const lines=wrapText(text,maxWidth,fontSize);
    setC(lightGray);doc.setFontSize(fontSize);doc.setFont('helvetica','normal');
    lines.forEach((line,i)=>{
      if(y+i*lineHeight > ph-15){doc.addPage();setF(dark);doc.rect(0,0,pw,ph,'F');y=20;setC(lightGray);doc.setFontSize(fontSize);}
      doc.text(line,x,y+i*lineHeight);
    });
    return y+lines.length*lineHeight;
  };
  
  // =============================================
  // PAGE 1: COVER
  // =============================================
  setF(dark);doc.rect(0,0,pw,ph,'F');
  // Top gradient bar
  setF(pink);doc.rect(0,0,pw,6,'F');
  // Logo area
  setF(darkCard);doc.roundedRect(pw/2-45,50,90,25,4,4,'F');
  doc.setFontSize(22);doc.setFont('helvetica','bold');setC(pink);doc.text('SocialPulse',pw/2,63,{align:'center'});
  doc.setFontSize(10);setC(accent);doc.text('PRO',pw/2+42,55);
  // Title
  doc.setFontSize(28);doc.setFont('helvetica','bold');setC(white);
  doc.text('LAPORAN MONITORING',pw/2,110,{align:'center'});
  doc.text('MEDIA SOSIAL',pw/2,125,{align:'center'});
  // Keyword pill
  setF(pink);doc.roundedRect(pw/2-50,140,100,18,9,9,'F');
  doc.setFontSize(16);setC(white);doc.text(keyword.toUpperCase(),pw/2,153,{align:'center'});
  // Period
  doc.setFontSize(12);setC(gray);doc.text(`Periode: ${dateFrom} - ${dateTo}`,pw/2,175,{align:'center'});
  // Stats
  doc.setFontSize(11);setC(white);
  doc.text(`${formatNum(stats.total)} Mentions  \u2022  ${formatNum(stats.views)} Views  \u2022  ${formatNum(engagement)} Engagement`,pw/2,195,{align:'center'});
  // Bottom bar
  setF(accent);doc.rect(0,ph-6,pw,6,'F');

  // =============================================
  // PAGE 2: EXECUTIVE SUMMARY
  // =============================================
  drawPage('EXECUTIVE SUMMARY',2);
  // Stats cards row
  const statsData=[
    {label:'Total Mentions',value:formatNum(stats.total),color:accent},
    {label:'Total Reach',value:formatNum(stats.views),color:tiktok},
    {label:'Engagement',value:formatNum(engagement),color:pink},
    {label:'Shares',value:formatNum(stats.shares),color:twitter},
    {label:'Positive',value:`${posPct}%`,color:green},
    {label:'Negative',value:`${negPct}%`,color:red}
  ];
  statsData.forEach((s,i)=>{
    const x=m+i*30;const bw=27;
    drawBox(x,30,bw,30,{topColor:s.color});
    doc.setFontSize(7);setC(gray);doc.text(s.label,x+bw/2,42,{align:'center'});
    doc.setFontSize(14);doc.setFont('helvetica','bold');setC(white);doc.text(s.value,x+bw/2,54,{align:'center'});
  });
  
  // Platform Distribution box
  drawBox(m,68,85,95,{topColor:accent,title:'Platform Distribution'});
  platEntries.slice(0,6).forEach(([p,c],i)=>{
    const y=88+i*13;const color=platformColors[p]||accent;
    setF(color);doc.circle(m+8,y-1,2.5,'F');
    doc.setFontSize(8);setC(white);doc.text(p.charAt(0).toUpperCase()+p.slice(1),m+14,y);
    setF([40,40,50]);doc.roundedRect(m+42,y-3,30,5,1,1,'F');
    setF(color);doc.roundedRect(m+42,y-3,(c/maxPlat)*30,5,1,1,'F');
    doc.setFontSize(7);doc.text(`${c} (${formatPct(c,total)}%)`,m+75,y);
  });
  
  // Sentiment box
  drawBox(105,68,85,95,{topColor:pink,title:'Sentiment Overview'});
  [{name:'Positive',value:stats.positive,color:green},{name:'Neutral',value:stats.neutral,color:yellow},{name:'Negative',value:stats.negative,color:red}].forEach((s,i)=>{
    const y=92+i*20;
    setF(s.color);doc.circle(112,y-1,4,'F');
    doc.setFontSize(10);setC(white);doc.text(s.name,120,y);
    doc.setFontSize(14);doc.setFont('helvetica','bold');doc.text(`${s.value}`,160,y);
    doc.setFontSize(8);doc.setFont('helvetica','normal');setC(gray);doc.text(`(${formatPct(s.value,total)}%)`,175,y);
  });
  doc.setFontSize(16);doc.setFont('helvetica','bold');setC(sentScore>=0?green:red);
  doc.text(`Score: ${sentScore>=0?'+':''}${sentScore}`,147,155,{align:'center'});
  
  // AI Executive Summary text
  let curY=172;
  if(aiData.executive_summary){
    drawBox(m,curY-4,pw-2*m,ph-curY-10,{topColor:accent});
    curY = drawParagraph(aiData.executive_summary, m+6, curY+8, pw-2*m-12, 8, 4);
  }

  // =============================================
  // PAGE 3: ENGAGEMENT ANALYSIS
  // =============================================
  drawPage('ENGAGEMENT ANALYSIS',3);
  // Engagement cards
  const engCards=[
    {label:'Total Likes',value:formatNum(stats.likes),color:accent},
    {label:'Total Comments',value:formatNum(stats.comments),color:green},
    {label:'Total Shares',value:formatNum(stats.shares),color:pink},
    {label:'Total Interactions',value:formatNum(engagement),color:tiktok}
  ];
  engCards.forEach((s,i)=>{
    const x=m+i*45;const bw=40;
    drawBox(x,30,bw,35,{topColor:s.color});
    doc.setFontSize(7);setC(gray);doc.text(s.label,x+bw/2,44,{align:'center'});
    doc.setFontSize(16);doc.setFont('helvetica','bold');setC(white);doc.text(s.value,x+bw/2,58,{align:'center'});
  });
  
  // Engagement Rate box
  drawBox(m,72,pw-2*m,30,{topColor:accent,title:'Engagement Rate'});
  doc.setFontSize(9);setC(lightGray);
  doc.text(`Average engagement per post: ${avgEngPerPost} interactions`,m+6,94);
  doc.text(`Likes per post: ${Math.round(stats.likes/total)} | Comments per post: ${Math.round(stats.comments/total)} | Shares per post: ${Math.round(stats.shares/total)}`,m+6,102);
  
  // Engagement by Platform
  drawBox(m,110,pw-2*m,80,{topColor:pink,title:'Engagement by Platform'});
  const platEng={};
  posts.forEach(p=>{if(!platEng[p.platform])platEng[p.platform]=0;platEng[p.platform]+=(p.likes||0)+(p.comments||0)+(p.shares||0);});
  const platEngEntries=Object.entries(platEng).sort((a,b)=>b[1]-a[1]);
  const maxEng=Math.max(...platEngEntries.map(([,v])=>v),1);
  platEngEntries.slice(0,6).forEach(([p,e],i)=>{
    const y=132+i*10;const color=platformColors[p]||accent;const barW=(e/maxEng)*110;
    doc.setFontSize(8);setC(white);doc.text(p,m+6,y);
    setF([40,40,50]);doc.roundedRect(m+40,y-4,110,6,1,1,'F');
    setF(color);doc.roundedRect(m+40,y-4,barW,6,1,1,'F');
    doc.setFontSize(7);setC(gray);doc.text(formatNum(e),m+155,y);
  });
  
  // AI Engagement Analysis
  curY=200;
  if(aiData.analisis_engagement){
    curY = drawParagraph(aiData.analisis_engagement, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 4: VOLUME & TRENDS
  // =============================================
  drawPage('VOLUME & TRENDS',4);
  drawBox(m,30,pw-2*m,120,{topColor:accent,title:'Mention Volume by Date & Platform'});
  const sortedDays=Object.keys(dailyMentions).sort();
  const allPlats=[...new Set(posts.map(p=>p.platform))];
  if(sortedDays.length>0){
    const chartW=pw-2*m-20;const chartH=80;const chartX=m+10;const chartY=55;
    const barGroupW=chartW/Math.max(sortedDays.length,1);
    const maxDay=Math.max(...sortedDays.map(d=>Object.values(dailyMentions[d]).reduce((a,b)=>a+b,0)),1);
    sortedDays.forEach((day,di)=>{
      const x=chartX+di*barGroupW;
      let stackY=chartY+chartH;
      allPlats.forEach(plat=>{
        const val=dailyMentions[day]?.[plat]||0;
        if(val>0){
          const barH=(val/maxDay)*chartH;
          setF(platformColors[plat]||accent);
          doc.rect(x+2,stackY-barH,barGroupW-4,barH,'F');
          stackY-=barH;
        }
      });
      doc.setFontSize(6);setC(gray);doc.text(day.split('-').slice(1).join('-'),x+barGroupW/2,chartY+chartH+8,{align:'center'});
    });
    // Legend
    allPlats.forEach((p,i)=>{
      const lx=m+12+i*30;
      setF(platformColors[p]||accent);doc.rect(lx,chartY+chartH+14,6,4,'F');
      doc.setFontSize(6);setC(gray);doc.text(p.charAt(0).toUpperCase()+p.slice(1),lx+8,chartY+chartH+17);
    });
  }
  // AI Trend Analysis
  curY=165;
  if(aiData.tren_mentions){
    curY = drawParagraph(aiData.tren_mentions, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 5: SENTIMENT ANALYSIS
  // =============================================
  drawPage('SENTIMENT ANALYSIS',5);
  // Sentiment cards
  drawBox(m,30,50,40,{topColor:green});
  doc.setFontSize(8);setC(gray);doc.text('Positive',m+25,44,{align:'center'});
  doc.setFontSize(20);doc.setFont('helvetica','bold');setC(green);doc.text(`${stats.positive}`,m+25,57,{align:'center'});
  doc.setFontSize(9);setC(gray);doc.text(`${posPct}%`,m+25,64,{align:'center'});
  
  drawBox(m+55,30,50,40,{topColor:yellow});
  doc.setFontSize(8);setC(gray);doc.text('Neutral',m+80,44,{align:'center'});
  doc.setFontSize(20);doc.setFont('helvetica','bold');setC(yellow);doc.text(`${stats.neutral}`,m+80,57,{align:'center'});
  doc.setFontSize(9);setC(gray);doc.text(`${neuPct}%`,m+80,64,{align:'center'});
  
  drawBox(m+110,30,50,40,{topColor:red});
  doc.setFontSize(8);setC(gray);doc.text('Negative',m+135,44,{align:'center'});
  doc.setFontSize(20);doc.setFont('helvetica','bold');setC(red);doc.text(`${stats.negative}`,m+135,57,{align:'center'});
  doc.setFontSize(9);setC(gray);doc.text(`${negPct}%`,m+135,64,{align:'center'});
  
  // Sentiment by platform
  drawBox(m,78,pw-2*m,85,{topColor:pink,title:'Sentiment by Platform'});
  Object.entries(platSentiment).forEach(([plat,sent],i)=>{
    const y=100+i*13;const total_p=sent.positive+sent.neutral+sent.negative;
    if(total_p===0)return;
    doc.setFontSize(8);setC(white);doc.text(plat,m+6,y);
    const barX=m+35;const barW=100;
    // Stacked bar
    let sx=barX;
    const pW=(sent.positive/total_p)*barW;const neW=(sent.neutral/total_p)*barW;const ngW=(sent.negative/total_p)*barW;
    setF(green);doc.rect(sx,y-4,pW,6,'F');sx+=pW;
    setF(yellow);doc.rect(sx,y-4,neW,6,'F');sx+=neW;
    if(ngW>0){setF(red);doc.rect(sx,y-4,ngW,6,'F');}
    doc.setFontSize(6);setC(gray);doc.text(`P:${sent.positive} N:${sent.neutral} Ng:${sent.negative}`,barX+barW+4,y);
  });
  
  // AI Sentiment Analysis
  curY=172;
  if(aiData.analisis_sentiment){
    curY = drawParagraph(aiData.analisis_sentiment, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 6: HOT ISSUES & HASHTAG ANALYSIS
  // =============================================
  drawPage('HOT ISSUES & HASHTAG ANALYSIS',6);
  // Treemap
  drawBox(m,30,pw-2*m,80,{topColor:accent,title:'Top Keywords (Treemap)'});
  const tmW=pw-2*m-12;const tmH=55;const tmX=m+6;const tmY=48;
  const totalKw=sortedKeywords.slice(0,12).reduce((s,[,v])=>s+v,0)||1;
  let tmCurX=tmX,tmCurY=tmY,tmRowH=0,tmRowW=0;
  sortedKeywords.slice(0,12).forEach(([tag,cnt],i)=>{
    const area=(cnt/totalKw)*tmW*tmH;
    const w=Math.max(Math.sqrt(area)*1.5,20);const h=Math.max(area/w,12);
    if(tmCurX+w>tmX+tmW){tmCurX=tmX;tmCurY+=tmRowH+2;tmRowH=0;}
    setF(treemapColors[i%treemapColors.length]);doc.roundedRect(tmCurX,tmCurY,Math.min(w,tmW-(tmCurX-tmX)),Math.min(h,16),1,1,'F');
    doc.setFontSize(Math.min(7,Math.max(5,w/5)));setC(white);doc.text(tag,tmCurX+2,tmCurY+Math.min(h,16)/2+2,{maxWidth:w-4});
    tmCurX+=w+2;tmRowH=Math.max(tmRowH,Math.min(h,16));
  });
  
  // Hashtag Clusters
  drawBox(m,118,pw-2*m,80,{topColor:pink,title:'Hashtag Clusters'});
  if(aiData.analisis_hashtag){
    drawParagraph(aiData.analisis_hashtag, m+6, 140, pw-2*m-12, 8, 4);
  } else {
    // Auto clusters
    const brandTags=sortedKeywords.filter(([t])=>t.includes(keyword.toLowerCase().replace(/\s/g,''))).slice(0,5);
    const otherTags=sortedKeywords.filter(([t])=>!t.includes(keyword.toLowerCase().replace(/\s/g,''))).slice(0,8);
    doc.setFontSize(8);doc.setFont('helvetica','bold');setC(accent);doc.text('Brand Identity',m+6,138);
    doc.setFont('helvetica','normal');setC(lightGray);doc.text(brandTags.map(([t,c])=>t+' ('+c+')').join(', '),m+6,146,{maxWidth:pw-2*m-12});
    doc.setFont('helvetica','bold');setC(pink);doc.text('Other',m+6,158);
    doc.setFont('helvetica','normal');setC(lightGray);doc.text(otherTags.map(([t,c])=>t+' ('+c+')').join(', '),m+6,166,{maxWidth:pw-2*m-12});
  }
  
  // Top 10 hashtag list
  drawBox(m,205,pw-2*m,80,{topColor:green,title:'Top 10 Trending Hashtags'});
  sortedKeywords.slice(0,10).forEach(([tag,cnt],i)=>{
    const y=225+i*7;
    doc.setFontSize(8);setC(white);doc.text(`${i+1}`,m+8,y);
    doc.text(tag,m+16,y);
    const barW=(cnt/sortedKeywords[0][1])*60;
    setF(treemapColors[i%treemapColors.length]);doc.roundedRect(m+80,y-3,barW,4,1,1,'F');
    setC(gray);doc.text(`${cnt}`,m+145,y);
  });

  // =============================================
  // PAGE 7: TIMELINE & ISSUE
  // =============================================
  drawPage('TIMELINE & ISSUE',7);
  const sortedPosts=[...posts].sort((a,b)=>(b.views||0)-(a.views||0));
  const postsByDate={};
  sortedPosts.forEach(p=>{
    const d=p.timestamp?new Date(p.timestamp).toLocaleDateString('id-ID',{weekday:'short',day:'numeric',month:'short'}):'Unknown';
    if(!postsByDate[d])postsByDate[d]=[];
    postsByDate[d].push(p);
  });
  curY=35;
  Object.entries(postsByDate).slice(0,5).forEach(([date,datePosts])=>{
    if(curY>ph-30)return;
    doc.setFontSize(9);doc.setFont('helvetica','bold');setC(pink);doc.text(date,m,curY);curY+=6;
    datePosts.slice(0,4).forEach(p=>{
      if(curY>ph-20)return;
      const color=platformColors[p.platform]||accent;
      setF(darkCard);doc.roundedRect(m,curY,pw-2*m,16,1,1,'F');
      setF(color);doc.rect(m,curY,3,16,'F');
      doc.setFontSize(7);doc.setFont('helvetica','bold');setC(white);doc.text(p.author||'',m+6,curY+6);
      doc.setFont('helvetica','normal');setC(lightGray);doc.text((p.content||'').substring(0,70)+'...',m+6,curY+12,{maxWidth:130});
      doc.setFontSize(6);setC(color);doc.text(`${formatNum(p.views||0)} views`,pw-m-25,curY+8);
      curY+=18;
    });
    curY+=4;
  });

  // =============================================
  // PAGE 8: SOCIAL NETWORK ANALYSIS
  // =============================================
  drawPage('SOCIAL NETWORK ANALYSIS',8);
  // Network visualization (simplified circles)
  drawBox(m,30,120,130,{topColor:accent,title:'Network Graph'});
  // Draw simplified SNA
  const snaCenter={x:m+60,y:105};
  setF(pink);doc.circle(snaCenter.x,snaCenter.y,12,'F');
  doc.setFontSize(6);setC(white);doc.text(keyword.substring(0,10),snaCenter.x,snaCenter.y+2,{align:'center'});
  topInfluencers.slice(0,6).forEach(([name,data],i)=>{
    const angle=(i/6)*Math.PI*2-Math.PI/2;const r=35;
    const nx=snaCenter.x+Math.cos(angle)*r;const ny=snaCenter.y+Math.sin(angle)*r;
    setD(gray);doc.setLineWidth(0.3);doc.line(snaCenter.x,snaCenter.y,nx,ny);
    setF(twitter);doc.circle(nx,ny,6,'F');
    doc.setFontSize(4);setC(white);doc.text(name.substring(0,8),nx,ny+1,{align:'center'});
  });
  
  // SNA sidebar info
  drawBox(m+125,30,pw-2*m-125,28,{topColor:twitter,title:'Top People'});
  topInfluencers.slice(0,3).forEach(([name],i)=>{
    doc.setFontSize(7);setC(white);doc.text(`${name}`,m+131,52+i*7);
  });
  drawBox(m+125,63,pw-2*m-125,28,{topColor:yellow,title:'Top Issues'});
  sortedKeywords.slice(0,3).forEach(([tag,cnt],i)=>{
    doc.setFontSize(7);setC(white);doc.text(`${tag} (${cnt})`,m+131,85+i*7);
  });
  drawBox(m+125,96,pw-2*m-125,28,{topColor:green,title:'Top Sources'});
  platEntries.slice(0,3).forEach(([plat,cnt],i)=>{
    doc.setFontSize(7);setC(white);doc.text(`${plat}: ${cnt}`,m+131,118+i*7);
  });
  drawBox(m+125,129,pw-2*m-125,28,{topColor:pink,title:'Top Locations'});
  sortedCities.slice(0,3).forEach(([city,cnt],i)=>{
    doc.setFontSize(7);setC(white);doc.text(`${city}: ${cnt}`,m+131,151+i*7);
  });

  // =============================================
  // PAGE 9: LOCATION ANALYSIS
  // =============================================
  drawPage('LOCATION ANALYSIS',9);
  drawBox(m,30,pw-2*m,140,{topColor:green,title:'Mentions by City'});
  const maxCity=sortedCities[0]?.[1]||1;
  sortedCities.forEach(([city,cnt],i)=>{
    const y=52+i*13;if(y>160)return;
    doc.setFontSize(9);doc.setFont('helvetica','bold');setC(white);doc.text(`${i+1}.`,m+8,y);
    doc.text(city,m+18,y);
    const barW=(cnt/maxCity)*80;
    setF([40,40,50]);doc.roundedRect(m+65,y-4,80,7,1,1,'F');
    setF(green);doc.roundedRect(m+65,y-4,barW,7,1,1,'F');
    doc.setFontSize(8);doc.text(`${cnt} mentions`,m+150,y);
  });
  // AI Location Analysis
  curY=180;
  if(aiData.analisis_lokasi){
    curY = drawParagraph(aiData.analisis_lokasi, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 10: MEDIA SHARE
  // =============================================
  drawPage('MEDIA SHARE',10);
  // Source Distribution
  drawBox(m,30,90,130,{topColor:accent,title:'Source Distribution'});
  const totalSources=Object.values(sources).reduce((a,b)=>a+b,0)||1;
  sortedSources.forEach(([src,cnt],i)=>{
    const y=52+i*12;if(y>150)return;
    const pct=formatPct(cnt,totalSources);
    setF(treemapColors[i%treemapColors.length]);doc.circle(m+8,y-1,3,'F');
    doc.setFontSize(7);setC(white);doc.text(src.substring(0,18),m+14,y);
    doc.text(`${pct}%`,m+75,y);
  });
  
  // Latest News
  drawBox(m+95,30,pw-2*m-95,130,{topColor:news,title:'Latest News'});
  const newsPosts=posts.filter(p=>p.platform==='news');
  if(newsPosts.length>0){
    newsPosts.slice(0,5).forEach((p,i)=>{
      const y=52+i*22;
      doc.setFontSize(7);doc.setFont('helvetica','bold');setC(white);
      doc.text((p.content||'').substring(0,45)+'...',m+101,y,{maxWidth:85});
      doc.setFontSize(6);doc.setFont('helvetica','normal');setC(gray);
      doc.text(`${p.source_name||'News'} \u2022 ${p.timestamp?new Date(p.timestamp).toLocaleDateString('id-ID'):''}`,m+101,y+10);
    });
  } else {
    doc.setFontSize(8);setC(gray);doc.text('No news articles found',m+115,80);
  }
  
  // AI Platform Analysis
  curY=172;
  if(aiData.analisis_platform){
    curY = drawParagraph(aiData.analisis_platform, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 11: INFLUENCER & SHARE OF VOICE
  // =============================================
  drawPage('INFLUENCER & SHARE OF VOICE',11);
  drawBox(m,30,pw-2*m,155,{topColor:pink,title:'Top Influencers by Reach'});
  topInfluencers.forEach(([name,data],i)=>{
    const y=52+i*14;if(y>175)return;
    const rankColors=[[255,215,0],[192,192,192],[205,127,50]];
    if(i<3){
      setF(rankColors[i]);doc.circle(m+10,y-1,4,'F');
      doc.setFontSize(7);setC(dark);doc.text((i+1).toString(),m+10,y+1,{align:'center'});
    } else {
      doc.setFontSize(8);setC(gray);doc.text((i+1).toString(),m+10,y,{align:'center'});
    }
    doc.setFontSize(8);doc.setFont('helvetica','bold');setC(white);doc.text(name.substring(0,20),m+20,y);
    doc.setFont('helvetica','normal');
    const sov=Math.round(data.views/totalReach*100);
    doc.setFontSize(7);setC(gray);
    doc.text(`${formatNum(data.views)} views \u2022 ${data.posts} posts \u2022 SOV ${sov}%`,m+20,y+7);
    // SOV bar
    setF([40,40,50]);doc.roundedRect(m+120,y-3,50,5,1,1,'F');
    setF(accent);doc.roundedRect(m+120,y-3,sov*0.5,5,1,1,'F');
    // Sentiment indicator
    const sentPct=Math.round(((data.positive-data.negative)/(data.posts||1))*100);
    setC(sentPct>=0?green:red);doc.text(`${sentPct>=0?'+':''}${sentPct}%`,m+175,y);
  });
  
  // AI Influencer analysis
  curY=195;
  if(aiData.analisis_influencer){
    curY = drawParagraph(aiData.analisis_influencer, m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 12: TOP PERFORMING CONTENT
  // =============================================
  drawPage('TOP PERFORMING CONTENT',12);
  const topMentions=[...posts].sort((a,b)=>(b.views||0)-(a.views||0)).slice(0,8);
  topMentions.forEach((p,i)=>{
    const y=35+i*30;if(y>ph-25)return;
    const color=platformColors[p.platform]||accent;
    drawBox(m,y,pw-2*m,27);
    setF(color);doc.rect(m,y,3,27,'F');
    // Rank
    doc.setFontSize(10);doc.setFont('helvetica','bold');setC(color);doc.text(`${i+1}.`,m+8,y+10);
    // Author
    doc.setFontSize(9);setC(white);doc.text(p.author||'Unknown',m+18,y+10);
    // Content preview
    doc.setFontSize(7);doc.setFont('helvetica','normal');setC(lightGray);
    doc.text((p.content||'').substring(0,90),m+18,y+18,{maxWidth:130});
    // Views
    doc.setFontSize(8);setC(color);doc.text(`${formatNum(p.views||0)} views`,pw-m-40,y+10);
    // Sentiment badge
    const sentColor=p.sentiment==='positive'?green:p.sentiment==='negative'?red:yellow;
    setF(sentColor);doc.roundedRect(pw-m-40,y+14,30,8,2,2,'F');
    doc.setFontSize(6);setC(white);doc.text((p.sentiment||'NEUTRAL').toUpperCase(),pw-m-25,y+20,{align:'center'});
  });

  // =============================================
  // PAGE 13: SWOT ANALYSIS
  // =============================================
  drawPage('SWOT ANALYSIS',13);
  const swot=aiData.swot||{strengths:[],weaknesses:[],opportunities:[],threats:[]};
  const swotLayout=[
    {title:'STRENGTHS',items:swot.strengths||[],color:green,x:m,y:30,w:(pw-2*m-5)/2,h:115},
    {title:'WEAKNESSES',items:swot.weaknesses||[],color:red,x:m+(pw-2*m-5)/2+5,y:30,w:(pw-2*m-5)/2,h:115},
    {title:'OPPORTUNITIES',items:swot.opportunities||[],color:accent,x:m,y:150,w:(pw-2*m-5)/2,h:115},
    {title:'THREATS',items:swot.threats||[],color:yellow,x:m+(pw-2*m-5)/2+5,y:150,w:(pw-2*m-5)/2,h:115}
  ];
  swotLayout.forEach(s=>{
    drawBox(s.x,s.y,s.w,s.h,{topColor:s.color,title:s.title});
    (s.items||[]).forEach((item,i)=>{
      const iy=s.y+24+i*18;if(iy>s.y+s.h-5)return;
      doc.setFontSize(7);setC(white);doc.setFont('helvetica','normal');
      doc.text('\u2022 '+item,s.x+8,iy,{maxWidth:s.w-16});
    });
  });

  // =============================================
  // PAGE 14: REKOMENDASI STRATEGIS
  // =============================================
  drawPage('REKOMENDASI STRATEGIS',14);
  drawBox(m,30,pw-2*m,240,{topColor:accent,title:'Actionable Recommendations'});
  const recos=aiData.recommendations||[];
  recos.forEach((r,i)=>{
    const y=52+i*35;if(y>ph-20)return;
    setF(darkCard);doc.roundedRect(m+8,y,pw-2*m-16,30,2,2,'F');
    setF(accent);doc.rect(m+8,y,3,30,'F');
    // Number circle
    setF(accent);doc.circle(m+18,y+7,5,'F');
    doc.setFontSize(8);doc.setFont('helvetica','bold');setC(white);doc.text(`${i+1}`,m+18,y+9,{align:'center'});
    // Text
    doc.setFontSize(8);doc.setFont('helvetica','normal');setC(lightGray);
    const lines=doc.splitTextToSize(r,pw-2*m-45);
    lines.slice(0,3).forEach((line,li)=>{doc.text(line,m+28,y+8+li*5);});
  });

  // =============================================
  // PAGE 15: ANALISA & KEY FINDINGS
  // =============================================
  drawPage('ANALISA & KEY FINDINGS',15);
  // Key Findings box
  drawBox(m,30,pw-2*m,80,{topColor:accent,title:'Key Findings'});
  const findings=aiData.key_findings||[];
  findings.forEach((f,i)=>{
    const y=52+i*11;if(y>100)return;
    doc.setFontSize(8);setC(white);doc.text('\u2022 '+f,m+8,y,{maxWidth:pw-2*m-16});
  });
  
  // Top Locations
  drawBox(m,118,85,80,{topColor:green,title:'Top Locations'});
  sortedCities.slice(0,5).forEach(([city,cnt],i)=>{
    doc.setFontSize(8);setC(white);doc.text(`${i+1}. ${city} \u2014 ${cnt} mentions`,m+8,140+i*12);
  });
  
  // Top Sources
  drawBox(m+90,118,pw-2*m-90,80,{topColor:twitter,title:'Top Sources'});
  platEntries.slice(0,5).forEach(([src,cnt],i)=>{
    doc.setFontSize(8);setC(white);doc.text(`${i+1}. ${src.charAt(0).toUpperCase()+src.slice(1)} \u2014 ${cnt} mentions`,m+98,140+i*12);
  });

  // Summary narrative
  curY=210;
  if(aiData.key_findings && aiData.key_findings.length>0){
    drawParagraph('Laporan ini menunjukkan bahwa '+keyword+' memiliki '+stats.total+' mentions dengan reach '+formatNum(stats.views)+'. Sentimen '+posPct+'% positif'+((negPct===0)?' dan zero negatif menunjukkan brand health yang sangat baik.':'.'), m, curY, pw-2*m, 8, 4);
  }

  // =============================================
  // PAGE 16: THANK YOU
  // =============================================
  drawPage('',16);
  // Actually draw full page
  setF(dark);doc.rect(0,0,pw,ph,'F');
  setF(pink);doc.rect(0,0,pw,6,'F');
  setF(accent);doc.rect(0,ph-6,pw,6,'F');
  doc.setFontSize(42);doc.setFont('helvetica','bold');setC(pink);
  doc.text('TERIMA KASIH',pw/2,ph/2-30,{align:'center'});
  doc.setFontSize(14);setC(white);
  doc.text('Laporan ini di-generate oleh SocialPulse Pro',pw/2,ph/2,{align:'center'});
  doc.setFontSize(11);setC(gray);
  doc.text(new Date().toLocaleDateString('id-ID',{weekday:'long',year:'numeric',month:'long',day:'numeric'}),pw/2,ph/2+20,{align:'center'});
  doc.setFontSize(9);
  doc.text(`Keyword: ${keyword} | Total: ${stats.total} mentions | Views: ${formatNum(stats.views)}`,pw/2,ph/2+40,{align:'center'});
  
  // ===== SAVE =====
  doc.save(`SocialPulse_${keyword.replace(/\s+/g,'_')}_${dateFrom}_${dateTo}.pdf`);
  toast('success','PDF Generated!','16-page comprehensive report with AI analysis downloaded');
}

'''

# Replace the function
new_content = content[:start_idx] + new_pdf_func + content[end_idx:]

with open('/home/claude/socialpulse/frontend/index.html', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("PDF function replaced successfully!")
print(f"Original file: {len(content)} chars")
print(f"New file: {len(new_content)} chars")
