package com.formatflow.offline

import android.graphics.*
import android.graphics.pdf.PdfDocument
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.gms.tasks.Task
import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.translate.*
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.Text
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import com.google.mlkit.vision.text.devanagari.DevanagariTextRecognizerOptions
import com.google.mlkit.vision.text.japanese.JapaneseTextRecognizerOptions
import com.google.mlkit.vision.text.korean.KoreanTextRecognizerOptions
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import kotlinx.coroutines.launch
import java.io.File
import kotlin.math.max
import kotlin.math.min

class MainActivity : AppCompatActivity() {
    data class Lang(val name:String,val code:String) { override fun toString()=name }
    private val languages=listOf(
        Lang("Arabic","ar"),Lang("Bengali","bn"),Lang("Chinese","zh"),Lang("Dutch","nl"),Lang("English","en"),Lang("Filipino","tl"),Lang("French","fr"),Lang("German","de"),Lang("Greek","el"),Lang("Hindi","hi"),Lang("Indonesian","id"),Lang("Italian","it"),Lang("Japanese","ja"),Lang("Korean","ko"),Lang("Malay","ms"),Lang("Polish","pl"),Lang("Portuguese","pt"),Lang("Russian","ru"),Lang("Spanish","es"),Lang("Swahili","sw"),Lang("Tamil","ta"),Lang("Thai","th"),Lang("Turkish","tr"),Lang("Ukrainian","uk"),Lang("Urdu","ur"),Lang("Vietnamese","vi"))
    private var input:Uri?=null
    private lateinit var source:Spinner;private lateinit var target:Spinner;private lateinit var progress:ProgressBar;private lateinit var status:TextView;private lateinit var translate:Button
    private val choose=registerForActivityResult(ActivityResultContracts.OpenDocument()){uri->uri?.let{input=it;contentResolver.takePersistableUriPermission(it,android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);findViewById<TextView>(R.id.fileName).text=fileName(it)}}
    private val create=registerForActivityResult(ActivityResultContracts.CreateDocument("application/pdf")){uri->uri?.let{runTranslation(it)}}

    override fun onCreate(savedInstanceState:Bundle?){super.onCreate(savedInstanceState);setContentView(R.layout.activity_main)
        source=findViewById(R.id.sourceLanguage);target=findViewById(R.id.targetLanguage);progress=findViewById(R.id.progress);status=findViewById(R.id.status);translate=findViewById(R.id.translate)
        val adapter=ArrayAdapter(this,R.layout.spinner_item,languages);adapter.setDropDownViewResource(R.layout.spinner_dropdown_item);source.adapter=adapter;target.adapter=adapter;source.setSelection(languages.indexOfFirst{it.code=="vi"});target.setSelection(languages.indexOfFirst{it.code=="en"})
        findViewById<Button>(R.id.choosePdf).setOnClickListener{choose.launch(arrayOf("application/pdf"))};translate.setOnClickListener{if(input==null)Toast.makeText(this,"Choose a PDF first",Toast.LENGTH_SHORT).show()else create.launch("translated-${fileName(input!!)}")}
    }
    private fun fileName(uri:Uri):String{contentResolver.query(uri,null,null,null,null)?.use{c->val i=c.getColumnIndex(OpenableColumns.DISPLAY_NAME);if(c.moveToFirst()&&i>=0)return c.getString(i)};return "document.pdf"}
    private fun runTranslation(output:Uri)=lifecycleScope.launch{
        translate.isEnabled=false
        val src=(source.selectedItem as Lang).code;val dst=(target.selectedItem as Lang).code
        if(src==dst){Toast.makeText(this@MainActivity,"Choose two different languages",Toast.LENGTH_SHORT).show();translate.isEnabled=true;return@launch}
        val options=TranslatorOptions.Builder().setSourceLanguage(src).setTargetLanguage(dst).build();val translator=Translation.getClient(options);val recognizer=recognizer(src)
        try{
            status.text="Downloading language model if needed…";translator.downloadModelIfNeeded(DownloadConditions.Builder().requireWifi().build()).await()
            processPdf(input!!,output,translator,recognizer,src,dst)
            progress.progress=100;status.text="Complete — translated PDF saved";Toast.makeText(this@MainActivity,"Translated PDF saved",Toast.LENGTH_LONG).show()
        }catch(e:Exception){status.text="Stopped: ${e.message}";Toast.makeText(this@MainActivity,e.message,Toast.LENGTH_LONG).show()}finally{translator.close();recognizer.close();translate.isEnabled=true}
    }
    private fun recognizer(code:String):TextRecognizer=when(code){"ja"->TextRecognition.getClient(JapaneseTextRecognizerOptions.Builder().build());"zh"->TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build());"ko"->TextRecognition.getClient(KoreanTextRecognizerOptions.Builder().build());"hi"->TextRecognition.getClient(DevanagariTextRecognizerOptions.Builder().build());else->TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)}

    private suspend fun processPdf(inputUri:Uri,outputUri:Uri,translator:Translator,recognizer:TextRecognizer,src:String,dst:String)=withContext(Dispatchers.IO){
        val fd=contentResolver.openFileDescriptor(inputUri,"r")?:error("Cannot open PDF");val renderer=PdfRenderer(fd);val pdf=PdfDocument();val continuation=mutableListOf<Pair<Int,String>>()
        for(i in 0 until renderer.pageCount){
            val page=renderer.openPage(i);val scale=2;val bitmap=Bitmap.createBitmap(page.width*scale,page.height*scale,Bitmap.Config.ARGB_8888);bitmap.eraseColor(Color.WHITE);page.render(bitmap,null,null,PdfRenderer.Page.RENDER_MODE_FOR_PRINT);page.close()
            runOnUiThread{progress.progress=(i*92/max(renderer.pageCount,1));status.text="Reading and translating page ${i+1} of ${renderer.pageCount}"}
            val detected=recognizer.process(InputImage.fromBitmap(bitmap,0)).await();val canvas=Canvas(bitmap)
            for(block in detected.textBlocks){val box=block.boundingBox?:continue;val protected=protect(block.text);val translated=translator.translateChunks(protected.first).restore(protected.second);val overflow=drawFitted(canvas,box,translated,dst,findViewById<CheckBox>(R.id.hyphenate).isChecked);if(overflow.isNotBlank()&&findViewById<CheckBox>(R.id.continuationPages).isChecked)continuation+=i+1 to overflow}
            val info=PdfDocument.PageInfo.Builder(bitmap.width,bitmap.height,pdf.pages.size+1).create();val outPage=pdf.startPage(info);outPage.canvas.drawBitmap(bitmap,0f,0f,null);pdf.finishPage(outPage);bitmap.recycle();getPreferences(MODE_PRIVATE).edit().putInt("last_page",i+1).apply()
        }
        continuation.forEach{(original,text)->val width=1240;val height=1754;val info=PdfDocument.PageInfo.Builder(width,height,pdf.pages.size+1).create();val p=pdf.startPage(info);p.canvas.drawColor(Color.WHITE);val note=Paint(Paint.ANTI_ALIAS_FLAG).apply{color=Color.GRAY;textSize=18f};p.canvas.drawText("Continued from page $original",60f,55f,note);drawFitted(p.canvas,Rect(60,85,width-60,height-70),text,dst,true);pdf.finishPage(p)}
        contentResolver.openOutputStream(outputUri,"w")!!.use{pdf.writeTo(it)};pdf.close();renderer.close();fd.close();getPreferences(MODE_PRIVATE).edit().remove("last_page").apply()
    }
    private suspend fun Translator.translateChunks(text:String):String{val pieces=text.split(Regex("(?<=[.!?。！？])\\s+")).fold(mutableListOf<String>()){a,s->if(a.isEmpty()||a.last().length+s.length>900)a.add(s)else a[a.lastIndex]=a.last()+" "+s;a};return pieces.joinToString(" "){translate(it).await()}}
    private fun protect(text:String):Pair<String,List<String>>{val saved=mutableListOf<String>();val regex=Regex("\\b[A-Z][\\p{L}'’.-]*-(?:san|chan|kun|sama|sensei)\\b",RegexOption.IGNORE_CASE);return regex.replace(text){saved+=it.value;"ZXQ${saved.lastIndex}QXZ"} to saved}
    private fun String.restore(saved:List<String>):String{var out=this;saved.forEachIndexed{i,v->out=out.replace(Regex("ZXQ\\s*$i\\s*QXZ",RegexOption.IGNORE_CASE),v)};return out}
    private fun drawFitted(canvas:Canvas,box:Rect,text:String,lang:String,hyphenate:Boolean):String{
        val rect=RectF(box);canvas.drawRect(rect,Paint().apply{color=Color.WHITE});val paint=Paint(Paint.ANTI_ALIAS_FLAG).apply{color=Color.rgb(20,20,20);typeface=Typeface.create("sans",Typeface.NORMAL)};var size=min(max(rect.height()*.7f,14f),42f);var lines:List<String>
        do{paint.textSize=size;lines=wrap(text,paint,rect.width(),lang,hyphenate);if(lines.size*size*1.18f<=rect.height())break;size-=1f}while(size>=12f)
        val maxLines=max(1,(rect.height()/(size*1.18f)).toInt());val shown=lines.take(maxLines);shown.forEachIndexed{i,line->val x=if(lang in listOf("ar","ur"))rect.right-paint.measureText(line) else rect.left;canvas.drawText(line,x,rect.top+size+i*size*1.18f,paint)};return lines.drop(maxLines).joinToString(" ")
    }
    private fun wrap(text:String,paint:Paint,width:Float,lang:String,hyphenate:Boolean):List<String>{val noSpace=lang in listOf("ja","zh","th");val words=if(noSpace)text.map{it.toString()}else text.split(Regex("\\s+"));val lines=mutableListOf<String>();var line="";val join=if(noSpace)"" else " ";for(word in words){val test=if(line.isEmpty())word else line+join+word;if(paint.measureText(test)<=width){line=test;continue};if(line.isNotEmpty())lines+=line;if(paint.measureText(word)<=width){line=word;continue};var part="";for(ch in word){val suffix=if(hyphenate&&!noSpace)"-" else "";if(part.isNotEmpty()&&paint.measureText(part+ch+suffix)>width){lines+=part+suffix;part=ch.toString()}else part+=ch};line=part};if(line.isNotEmpty())lines+=line;return lines}
}
